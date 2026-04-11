"""Startup recovery scan for the mars-runtime supervisor.

When the supervisor process starts, it walks every session directory
under ``workspace_root`` (``/workspace`` in production), reads the
persisted :class:`PersistedSessionHandle`, and classifies each
session so the control plane can show the right status to the user:

* ``dead`` — the recorded pid is gone. Mark ``needs_restart``.
* ``orphan_alive`` — the pid is still alive BUT does not match the
  claude/codex cmdline check. Someone else got the pid (PID reuse
  after reboot) or the old supervisor spawned something unexpected.
  Mark ``needs_restart`` and log a loud warning.
* ``reattach_candidate`` — the pid is alive AND the cmdline looks
  like a claude/codex binary. v1 intentionally does NOT auto-attach
  to the running subprocess (no asyncio primitive to adopt a
  foreign stdout pipe cleanly, and double-running is catastrophic).
  Mark ``needs_restart`` with a "was still running" note so the
  user can explicitly decide whether to kill the live one or leave
  it alone.
* ``corrupt_handle`` — the handle file exists but is unparseable.
  Mark ``needs_restart`` with "corrupt handle" reason.

**NEVER auto-spawn on recovery.** The epic plan is explicit: double
runs (two claude processes editing the same repo) are worse than
any inconvenience from requiring a manual click.

This module deliberately returns a ``list[RecoveredSession]`` rather
than mutating a manager — the caller (the FastAPI lifespan hook or
a startup script) decides how to surface the results to the control
plane. Tests exercise pure functions with fake workspace dirs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from session.handle import (
    HANDLE_FILENAME,
    PersistedSessionHandle,
    is_claude_or_codex_process,
    is_pid_alive,
    scan_workspace_handles,
)

__all__ = [
    "RecoveredSession",
    "RecoveryStatus",
    "classify_session",
    "recover_workspace",
]

_log = logging.getLogger("mars.runtime.recovery")

#: Narrow enum of what the recovery scan can decide about a session.
#: Every case maps to "mark as needs_restart" at the control plane
#: layer — the distinction is the *reason* displayed to the admin.
RecoveryStatus = Literal[
    "dead",
    "orphan_alive",
    "reattach_candidate",
    "corrupt_handle",
]


@dataclass
class RecoveredSession:
    """One session found during the startup scan."""

    session_dir: Path
    status: RecoveryStatus
    #: Loaded handle, or None if the handle file was corrupt.
    handle: PersistedSessionHandle | None
    #: Human-readable reason for status, surfaced in logs + UI.
    reason: str

    @property
    def needs_restart(self) -> bool:
        """Every recovery outcome means "UI should offer Resume"."""
        return True

    @property
    def session_id(self) -> str | None:
        return self.handle.session_id if self.handle is not None else None


def classify_session(
    session_dir: Path,
    handle: PersistedSessionHandle | None,
    *,
    is_alive: Callable[[int], bool] = is_pid_alive,
    is_claude_or_codex: Callable[[int], bool] = is_claude_or_codex_process,
) -> RecoveredSession:
    """Pure classification of one session dir + optional handle.

    The two callables are injectable so tests can mock out PID /
    cmdline checks without spawning real processes.
    """
    if handle is None:
        return RecoveredSession(
            session_dir=session_dir,
            status="corrupt_handle",
            handle=None,
            reason=(
                f"handle file at {session_dir / HANDLE_FILENAME} is "
                "missing or unparseable"
            ),
        )

    pid_alive = is_alive(handle.pid)
    if not pid_alive:
        return RecoveredSession(
            session_dir=session_dir,
            status="dead",
            handle=handle,
            reason=f"pid {handle.pid} is not running",
        )

    if not is_claude_or_codex(handle.pid):
        return RecoveredSession(
            session_dir=session_dir,
            status="orphan_alive",
            handle=handle,
            reason=(
                f"pid {handle.pid} is alive but its cmdline does not "
                "match claude/codex — likely PID reuse after reboot"
            ),
        )

    return RecoveredSession(
        session_dir=session_dir,
        status="reattach_candidate",
        handle=handle,
        reason=(
            f"pid {handle.pid} is still running claude/codex; v1 does "
            "not auto-reattach — admin must decide whether to kill "
            "the live process and start fresh"
        ),
    )


def recover_workspace(
    workspace_root: str | Path,
    *,
    is_alive: Callable[[int], bool] = is_pid_alive,
    is_claude_or_codex: Callable[[int], bool] = is_claude_or_codex_process,
) -> list[RecoveredSession]:
    """Scan ``workspace_root`` and classify every session found.

    Never spawns a subprocess, never mutates handle files. Returns
    a list sorted by session directory name so logs are stable and
    replayable. Callers (supervisor lifespan hook + future tests)
    use the result to tell the control plane what state the machine
    came up in.

    Empty / missing ``workspace_root`` returns ``[]``.
    """
    pairs = scan_workspace_handles(workspace_root)
    results: list[RecoveredSession] = []
    for session_dir, handle in pairs:
        outcome = classify_session(
            session_dir,
            handle,
            is_alive=is_alive,
            is_claude_or_codex=is_claude_or_codex,
        )
        _log_classification(outcome)
        results.append(outcome)
    return results


def _log_classification(outcome: RecoveredSession) -> None:
    if outcome.status == "dead":
        _log.info(
            "recovery: %s is dead (pid %s) — will mark needs_restart",
            outcome.session_dir,
            outcome.handle.pid if outcome.handle else "?",
        )
    elif outcome.status == "orphan_alive":
        _log.warning(
            "recovery: %s has a live pid %s with an unexpected cmdline "
            "— PID reuse suspected, marking needs_restart",
            outcome.session_dir,
            outcome.handle.pid if outcome.handle else "?",
        )
    elif outcome.status == "reattach_candidate":
        _log.warning(
            "recovery: %s is still running claude/codex (pid %s) — "
            "v1 does not auto-reattach; admin must choose",
            outcome.session_dir,
            outcome.handle.pid if outcome.handle else "?",
        )
    elif outcome.status == "corrupt_handle":
        _log.warning(
            "recovery: %s has a corrupt handle file — marking needs_restart",
            outcome.session_dir,
        )
