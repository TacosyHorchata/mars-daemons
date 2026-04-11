"""In-memory :class:`SessionManager` for the Mars runtime supervisor.

v1 scope (Story 1.4): single machine, single process, sessions live in
a dict keyed by Mars session id. Crash-recovery to volume state is
Story 5.x. Event forwarding to the control plane is Epic 2.

Design principles
-----------------

* **One subprocess per session.** Each session owns exactly one
  ``claude`` subprocess. When the supervisor decides the session is
  done it calls :meth:`SessionManager.kill`, which SIGKILLs the process
  (for determinism — no graceful shutdown dance in v1) and awaits
  ``proc.wait()`` so the child is reaped and leaves no zombie.

* **Spawn is pluggable.** The constructor takes an injectable
  ``spawn_fn`` so tests can substitute ``/bin/sleep`` for the real
  ``claude`` binary and avoid spending Claude Max quota. Production
  wires :func:`session.claude_code.spawn_claude_code`.

* **Concurrency model.** Session dict mutations happen from a single
  asyncio event loop — no threading, no multi-loop semantics. A small
  :class:`asyncio.Lock` guards the kill path so two concurrent kill
  calls for the same session do not both try to ``pop`` and wait.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal

_manager_log = logging.getLogger("mars.runtime.manager")

from schema.agent import AgentConfig

from .claude_code import spawn_claude_code

__all__ = [
    "DEFAULT_WORKSPACE_ROOT",
    "MAX_SESSIONS_PER_MACHINE",
    "SessionCapReachedError",
    "SessionHandle",
    "SessionIdLogFilter",
    "SessionManager",
    "SessionStatus",
    "SpawnFn",
    "current_session_id",
    "install_session_log_filter",
]

#: Hard cap on concurrent sessions per Fly machine in v1. v1.1 bumps
#: to 10 with a dynamic check against machine memory; v2 is fully
#: dynamic. Documented as "a v1 number — don't rathole on why 3" in
#: Epic 5's notes.
MAX_SESSIONS_PER_MACHINE = 3

#: Default workspace root on Fly machines. Each session gets its own
#: ``<root>/<session_id>`` subdirectory enforced as the subprocess
#: ``cwd`` so sessions cannot read or write each other's files.
DEFAULT_WORKSPACE_ROOT = "/workspace"


class SessionCapReachedError(RuntimeError):
    """Raised by :meth:`SessionManager.spawn` when the machine is
    already running :data:`MAX_SESSIONS_PER_MACHINE` sessions.

    The supervisor's ``POST /sessions`` handler translates this to
    HTTP 429 with a clean message — the UI can show "machine full,
    wait for an existing session to finish".
    """


# ---------------------------------------------------------------------------
# Structured session-id logging — stdlib only, no structlog dep
# ---------------------------------------------------------------------------

#: ContextVar holding the session id for the current asyncio task.
#: The supervisor's event pump sets this when it wraps per-session
#: work; log records emitted from within that task get automatically
#: tagged with ``session_id`` via :class:`SessionIdLogFilter`.
current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mars_session_id", default=None
)


class SessionIdLogFilter(logging.Filter):
    """Stdlib logging filter that stamps ``session_id`` onto every
    record whose origin task has set :data:`current_session_id`.

    Use with any log format that references ``%(session_id)s`` — the
    filter guarantees the attribute is always present (set to
    ``"-"`` when the context var is unset) so format strings don't
    blow up with ``KeyError`` outside session-scoped code.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        sid = current_session_id.get()
        record.session_id = sid if sid is not None else "-"
        return True


def install_session_log_filter(logger: logging.Logger | None = None) -> SessionIdLogFilter:
    """Attach a :class:`SessionIdLogFilter` to ``logger`` (or the root).

    Returns the installed filter so callers can detach it in tests.
    Idempotent — calling twice on the same logger adds two filters,
    so tests that need a clean slate should detach the returned
    filter when done.
    """
    target = logger if logger is not None else logging.getLogger()
    flt = SessionIdLogFilter()
    target.addFilter(flt)
    return flt


#: Observable terminal states. Kept precise so downstream UIs can
#: distinguish "the daemon finished what it was doing" from "we SIGKILLed
#: it on a shutdown request" from "it crashed".
SessionStatus = Literal[
    "running",
    "exited_clean",  # subprocess exited with returncode == 0
    "killed",  # supervisor-requested kill completed (SIGKILL seen)
    "exited_error",  # subprocess exited with non-zero returncode
    "kill_timeout",  # SIGKILL sent but wait() timed out — orphan, tombstoned
]

#: Spawn-function contract: ``(config, session_id) -> Process``. The
#: production wiring is :func:`session.claude_code.spawn_claude_code`;
#: tests inject a fake that runs ``sleep``.
SpawnFn = Callable[[AgentConfig, str], Awaitable[asyncio.subprocess.Process]]


@dataclass
class SessionHandle:
    """Everything the supervisor needs to know about a live session.

    Held in memory only in v1.4 — :class:`SessionManager` does not
    persist handles to the Fly volume yet (that is Story 5.1).
    """

    session_id: str
    name: str
    description: str
    config: AgentConfig
    process: asyncio.subprocess.Process
    started_at: datetime
    status: SessionStatus = "running"
    terminated_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def pid(self) -> int:
        """OS pid of the claude subprocess."""
        return self.process.pid

    @property
    def is_alive(self) -> bool:
        """True iff the subprocess has not yet exited."""
        return self.process.returncode is None


def _new_session_id() -> str:
    """Mars-side session id. Distinct from Claude Code's internal id."""
    return f"mars-{uuid.uuid4()}"


class SessionManager:
    """In-memory registry of live Claude Code sessions.

    Tests pass a fake ``spawn_fn`` via the constructor so they never
    launch a real ``claude`` subprocess. Production supervisors default
    to :func:`session.claude_code.spawn_claude_code`.
    """

    def __init__(
        self,
        spawn_fn: SpawnFn | None = None,
        *,
        max_sessions: int = MAX_SESSIONS_PER_MACHINE,
        workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    ):
        if max_sessions <= 0:
            raise ValueError(f"max_sessions must be positive, got {max_sessions}")
        self._spawn_fn: SpawnFn = spawn_fn or spawn_claude_code
        self._max_sessions = max_sessions
        self._workspace_root = Path(workspace_root)
        self._sessions: dict[str, SessionHandle] = {}
        #: Handles for sessions whose kill() timed out — SIGKILL sent but
        #: wait() never returned. We keep the reference so the pid is
        #: visible to observability/alerting even though we can no longer
        #: guarantee it is reaped on this process lifetime.
        self._orphaned: list[SessionHandle] = []
        self._lock = asyncio.Lock()

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def session_workdir(self, session_id: str) -> Path:
        """Return the per-session working directory path (not yet created)."""
        return self._workspace_root / session_id

    @property
    def orphaned(self) -> list[SessionHandle]:
        """Handles whose SIGKILL timed out. For observability only."""
        return list(self._orphaned)

    # ------------------------------------------------------------------
    # Public API (called by supervisor.py)
    # ------------------------------------------------------------------

    async def spawn(self, config: AgentConfig) -> SessionHandle:
        """Spawn a new session for ``config`` and register it.

        Story 5.2 additions:

        * Hard-caps the number of concurrent sessions at
          :data:`max_sessions` and raises :class:`SessionCapReachedError`
          when full. The supervisor's ``POST /sessions`` maps this to
          HTTP 429.
        * Creates the per-session working directory at
          ``<workspace_root>/<session_id>/`` BEFORE spawning so the
          subprocess has a legal ``cwd`` to land in. The directory
          tracks on the :class:`SessionHandle` for downstream cleanup.
        """
        if len(self._sessions) >= self._max_sessions:
            raise SessionCapReachedError(
                f"max {self._max_sessions} concurrent sessions already running "
                f"on this machine — wait for one to finish or kill it"
            )

        session_id = _new_session_id()
        workdir = self.session_workdir(session_id)
        workdir_metadata: dict[str, str] = {}
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            workdir_metadata["session_workdir"] = str(workdir)
        except OSError as exc:
            # Non-fatal: on local dev / unit tests the default
            # ``/workspace`` root is not writable. Log and proceed —
            # the isolation guarantee only applies when the
            # workspace_root is writable (i.e. inside a real Fly
            # machine). Tests can opt into isolation by passing
            # ``workspace_root=tmp_path`` at construction time.
            _manager_log.warning(
                "failed to create per-session workdir %s: %s — "
                "session will spawn without isolated cwd",
                workdir,
                exc,
            )

        proc = await self._spawn_fn(config, session_id)
        handle = SessionHandle(
            session_id=session_id,
            name=config.name,
            description=config.description,
            config=config,
            process=proc,
            started_at=datetime.now(timezone.utc),
            status="running",
            metadata=workdir_metadata,
        )
        self._sessions[session_id] = handle
        return handle

    def list(self) -> list[SessionHandle]:
        """Return every currently-registered session handle."""
        return list(self._sessions.values())

    def get(self, session_id: str) -> SessionHandle | None:
        """Fetch one handle by Mars session id, or ``None``."""
        return self._sessions.get(session_id)

    async def kill(self, session_id: str, *, timeout: float = 5.0) -> bool:
        """Terminate a session's subprocess and remove it from the dict.

        Returns ``True`` if a session was found and killed (or was
        already dead when we claimed it), ``False`` if the session id
        is unknown. ``pop`` happens under a lock so two concurrent
        ``kill()`` calls for the same session cannot both claim the
        handle — exactly one wins.

        Awaits ``proc.wait()`` outside the lock so the child is reaped
        before we return. If the wait times out (SIGKILL not honored
        within ``timeout`` seconds) the handle is moved to
        :attr:`orphaned` so observability can still see the pid.
        """
        async with self._lock:
            handle = self._sessions.pop(session_id, None)
        if handle is None:
            return False

        proc = handle.process
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                # Race: child exited between the returncode check and
                # the kill call. Fine — it's already dead.
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # SIGKILL should be instant unless the child is in
                # uninterruptible sleep or a ptrace stop. Tombstone the
                # handle so the pid remains visible.
                handle.status = "kill_timeout"
                handle.terminated_at = datetime.now(timezone.utc)
                self._orphaned.append(handle)
                return True

        handle.status = _classify_returncode(proc.returncode)
        handle.terminated_at = datetime.now(timezone.utc)
        return True

    async def restart(
        self, session_id: str, *, timeout: float = 5.0
    ) -> SessionHandle | None:
        """Restart a session's subprocess *in place*, preserving the
        :attr:`session_id`.

        Story 6.4's CLAUDE.md admin-edit flow needs the session to
        survive a prompt update so the browser URL (keyed on
        session_id) doesn't have to follow a redirect after every
        admin tweak. Instead of going through
        :meth:`kill` + :meth:`spawn` (which would mint a new id),
        this method:

        1. Signals the running subprocess with SIGKILL (authoritative
           stop — no graceful shutdown dance).
        2. Awaits the reap via ``proc.wait()`` with ``timeout``.
        3. Spawns a fresh subprocess using the same stored config.
        4. Updates the handle in place and returns it.

        Returns ``None`` if ``session_id`` is unknown. Raises whatever
        the spawn function raises (file not found, etc.) — the caller
        is the supervisor endpoint, which turns that into a 500 the
        admin UI can display.
        """
        handle = self._sessions.get(session_id)
        if handle is None:
            return None

        old_proc = handle.process
        if old_proc.returncode is None:
            try:
                old_proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(old_proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # SIGKILL didn't land within the timeout — tombstone
                # the old process and keep going. This matches
                # kill()'s orphan-tracking behavior.
                self._orphaned.append(
                    SessionHandle(
                        session_id=f"{session_id}-orphan",
                        name=handle.name,
                        description=handle.description,
                        config=handle.config,
                        process=old_proc,
                        started_at=handle.started_at,
                        status="kill_timeout",
                        terminated_at=datetime.now(timezone.utc),
                    )
                )

        new_proc = await self._spawn_fn(handle.config, session_id)
        handle.process = new_proc
        handle.started_at = datetime.now(timezone.utc)
        handle.status = "running"
        handle.terminated_at = None
        return handle

    async def shutdown(self) -> None:
        """Kill every registered session concurrently.

        Uses :func:`asyncio.gather` with ``return_exceptions=True`` so
        one stuck session cannot burn ``N * timeout`` seconds of
        supervisor shutdown time. Exceptions are swallowed — the
        supervisor is already tearing down.
        """
        session_ids = list(self._sessions.keys())
        if not session_ids:
            return
        await asyncio.gather(
            *(self.kill(sid) for sid in session_ids),
            return_exceptions=True,
        )


def _classify_returncode(returncode: int | None) -> SessionStatus:
    """Map a subprocess returncode onto a Mars :data:`SessionStatus`.

    * ``None`` — still running (should not happen here, but guard).
    * ``0`` — clean exit.
    * ``-SIGKILL`` — supervisor-requested kill observed.
    * anything else (non-zero exit, other signals) — error.
    """
    if returncode is None:
        return "running"
    if returncode == 0:
        return "exited_clean"
    if returncode == -signal.SIGKILL:
        return "killed"
    return "exited_error"
