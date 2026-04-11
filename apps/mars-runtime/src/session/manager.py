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
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

from schema.agent import AgentConfig

from .claude_code import spawn_claude_code

__all__ = [
    "SessionHandle",
    "SessionManager",
    "SessionStatus",
    "SpawnFn",
]


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

    def __init__(self, spawn_fn: SpawnFn | None = None):
        self._spawn_fn: SpawnFn = spawn_fn or spawn_claude_code
        self._sessions: dict[str, SessionHandle] = {}
        #: Handles for sessions whose kill() timed out — SIGKILL sent but
        #: wait() never returned. We keep the reference so the pid is
        #: visible to observability/alerting even though we can no longer
        #: guarantee it is reaped on this process lifetime.
        self._orphaned: list[SessionHandle] = []
        self._lock = asyncio.Lock()

    @property
    def orphaned(self) -> list[SessionHandle]:
        """Handles whose SIGKILL timed out. For observability only."""
        return list(self._orphaned)

    # ------------------------------------------------------------------
    # Public API (called by supervisor.py)
    # ------------------------------------------------------------------

    async def spawn(self, config: AgentConfig) -> SessionHandle:
        """Spawn a new session for ``config`` and register it."""
        session_id = _new_session_id()
        proc = await self._spawn_fn(config, session_id)
        handle = SessionHandle(
            session_id=session_id,
            name=config.name,
            description=config.description,
            config=config,
            process=proc,
            started_at=datetime.now(timezone.utc),
            status="running",
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
