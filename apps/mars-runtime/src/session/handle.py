"""Persistent session handle: what the supervisor writes to disk.

Story 5.1 — survives supervisor crashes via atomic write (tmp +
fsync + rename), PID liveness check via ``os.kill(pid, 0)``, and a
cmdline verification step so PID reuse after a reboot cannot be
mistaken for a live Mars session.

The in-memory :class:`~session.manager.SessionHandle` is a
*runtime* handle (holds an ``asyncio.subprocess.Process`` reference).
This :class:`PersistedSessionHandle` is the *on-disk* serialization
of that state so a fresh supervisor can reconstruct what it was
running before it died. The two intentionally use different names
to prevent confusion.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "HANDLE_FILENAME",
    "PersistedSessionHandle",
    "atomic_write_handle",
    "find_process_cmdline",
    "is_claude_or_codex_process",
    "is_pid_alive",
    "read_handle",
    "scan_workspace_handles",
]

#: Filename the supervisor uses inside ``/workspace/<session-id>/``.
#: Kept as a constant so the scan logic + writer stay in lockstep.
HANDLE_FILENAME = "supervisor_handle.json"


@dataclass
class PersistedSessionHandle:
    """JSON-serializable snapshot of a session for crash recovery.

    This is a *separate* shape from the in-memory
    :class:`session.manager.SessionHandle`:

    * No subprocess reference (can't serialize a Process object).
    * No ``status`` field — the recovery scan derives status from
      PID liveness, not from whatever the old supervisor wrote
      before it died.
    * Includes ``last_heartbeat`` so the supervisor can tell a
      fresh crash from a stale handle left behind by a session
      that crashed days ago.
    """

    session_id: str
    agent_name: str
    pid: int
    started_at: str
    last_heartbeat: str
    agent_yaml_path: str
    workdir: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersistedSessionHandle":
        """Build a handle from a loaded JSON dict.

        Unknown fields are ignored (forward-compatible); missing
        required fields raise :class:`KeyError`.
        """
        return cls(
            session_id=data["session_id"],
            agent_name=data["agent_name"],
            pid=int(data["pid"]),
            started_at=data["started_at"],
            last_heartbeat=data["last_heartbeat"],
            agent_yaml_path=data["agent_yaml_path"],
            workdir=data["workdir"],
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_heartbeat(self, now: datetime | None = None) -> "PersistedSessionHandle":
        """Return a new handle with ``last_heartbeat`` bumped to now."""
        ts = (now or datetime.now(timezone.utc)).isoformat()
        return PersistedSessionHandle(
            session_id=self.session_id,
            agent_name=self.agent_name,
            pid=self.pid,
            started_at=self.started_at,
            last_heartbeat=ts,
            agent_yaml_path=self.agent_yaml_path,
            workdir=self.workdir,
            metadata=dict(self.metadata),
        )


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def atomic_write_handle(
    handle: PersistedSessionHandle, directory: str | Path
) -> Path:
    """Write ``handle`` to ``directory/supervisor_handle.json`` atomically.

    Pattern: write to ``<file>.tmp``, ``fsync``, then rename onto
    the final path. Guarantees that a crash mid-write never leaves
    a partially-written JSON blob the recovery scan would choke on.

    Returns the final file path.
    """
    dir_path = Path(directory)
    dir_path.mkdir(parents=True, exist_ok=True)
    final_path = dir_path / HANDLE_FILENAME
    tmp_path = dir_path / f"{HANDLE_FILENAME}.tmp"

    payload = json.dumps(handle.to_dict(), indent=2) + "\n"
    # Open low-level so we can fsync before rename.
    fd = os.open(
        str(tmp_path),
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp_path), str(final_path))
    return final_path


def read_handle(directory: str | Path) -> PersistedSessionHandle | None:
    """Load a persisted handle from ``directory``. Returns ``None`` if
    the file doesn't exist, is malformed, or lacks required fields.

    The recovery scan prefers "mark as needs_restart" over crashing
    on a corrupt file, so all parse failures map to ``None`` rather
    than raising.
    """
    path = Path(directory) / HANDLE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return PersistedSessionHandle.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# PID liveness + cmdline check
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` idiom — no exception = process exists.

    ``ProcessLookupError`` = pid not in the process table.
    ``PermissionError`` = pid exists but owned by another user.
    OverflowError / OSError on weird pid values are treated as dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, OSError):
        return False
    return True


def find_process_cmdline(pid: int) -> str:
    """Return the command line of ``pid`` or empty string on failure.

    Linux: reads ``/proc/<pid>/cmdline`` (null-separated args).
    macOS / BSD: falls back to ``ps -o command= -p <pid>``.
    Returns "" if neither method works (pid dead, permission
    denied, etc.) — the caller treats empty as "can't verify
    ownership, treat as dead".
    """
    proc_file = Path(f"/proc/{pid}/cmdline")
    if proc_file.exists():
        try:
            raw = proc_file.read_bytes()
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            return ""
    # macOS fallback
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def is_claude_or_codex_process(pid: int) -> bool:
    """Verify ``pid`` is running a ``claude`` or ``codex`` binary.

    Guards against PID reuse: after a machine reboot, the OS might
    hand the old Mars PID to an unrelated process. Before
    reattaching, we check the command line contains ``claude`` or
    ``codex`` as a whole path segment / token.
    """
    cmdline = find_process_cmdline(pid)
    if not cmdline:
        return False
    lowered = cmdline.lower()
    # Match the executable name as a word, not just a substring —
    # so "claudetronic" or "codexplorer" don't count.
    tokens = lowered.replace("/", " ").replace("\\", " ").split()
    for token in tokens:
        # Strip common flags / path suffixes
        base = token.split("@")[0]
        if base.endswith(".exe"):
            base = base[:-4]
        if base == "claude" or base == "codex":
            return True
    return False


# ---------------------------------------------------------------------------
# Workspace scan
# ---------------------------------------------------------------------------


def scan_workspace_handles(
    workspace_root: str | Path,
) -> list[tuple[Path, PersistedSessionHandle | None]]:
    """Walk ``<workspace_root>/*`` and load each session's handle.

    Returns a list of ``(session_dir, handle_or_none)`` pairs so
    callers can tell "handle file missing / corrupted" (None) apart
    from "handle loaded, verify PID liveness separately".
    """
    root = Path(workspace_root)
    if not root.is_dir():
        return []
    out: list[tuple[Path, PersistedSessionHandle | None]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / HANDLE_FILENAME).exists():
            continue
        out.append((entry, read_handle(entry)))
    return out
