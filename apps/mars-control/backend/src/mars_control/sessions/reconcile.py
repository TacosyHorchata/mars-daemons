"""Control-plane reconciliation — cross-check machine state vs DB.

Story 5.4 — when a Fly machine reconnects after a gap (supervisor
OOM, host migration, network partition), the control plane's
in-memory / persisted view of live sessions diverges from the
machine's actual state. This module provides a pure, stateless
function that compares the two views and returns a structured
report the control plane can surface to the admin UI.

The shape is deliberately dict-in / dataclass-out so callers can
feed it from any source (the mars-control SQLite store today, an
Epic 5 SessionStore tomorrow, a hand-rolled test fixture) without
coupling to a specific persistence layer.

**Never automatically acts.** The report only *describes* divergence —
it is the admin UI's job (or a future auto-healer) to call the
supervisor's resume / kill endpoints based on the user's choice.
The epic plan is explicit: "NEVER auto-resume. Always require
human confirmation via the Resume button."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

__all__ = [
    "KnownSessionState",
    "MachineSessionState",
    "ReconcileReport",
    "ReconcileStatusChange",
    "reconcile_machine_state",
]


#: Terminal statuses we expect on either side. The control plane
#: tracks "running" or "needs_restart"; the machine reports its own
#: SessionManager status set (running / exited_* / killed / ...).
#: The reconciler does not enforce the sets — mismatched values just
#: show up as a status_changes entry.


@dataclass(frozen=True)
class KnownSessionState:
    """What the control plane currently thinks about a session."""

    session_id: str
    agent_name: str
    status: str
    #: Supervisor base URL hosting the session (for the Resume
    #: action). Optional because the control plane may have
    #: forgotten it during a crash — in that case ``reconcile`` is
    #: the recovery surface that fills it back in.
    supervisor_url: str | None = None


@dataclass(frozen=True)
class MachineSessionState:
    """What one mars-runtime machine reports via GET /sessions."""

    session_id: str
    agent_name: str
    status: str
    supervisor_url: str


@dataclass(frozen=True)
class ReconcileStatusChange:
    session_id: str
    previous: str
    current: str


@dataclass
class ReconcileReport:
    """Structured output of a reconciliation scan.

    Four buckets of divergence the UI can render distinctly:

    * ``in_sync`` — session appears on both sides with matching
      status. No action needed.
    * ``status_changes`` — session appears on both sides but status
      differs. The machine wins (it is the authoritative source),
      so the control plane should update its row.
    * ``disappeared`` — session is in the control-plane view but
      the machine does NOT know about it. This is the "machine
      crashed without reporting it" or "session was killed while
      control plane was offline" case. Mark ``needs_restart`` in
      the control plane and expose Resume in the UI.
    * ``appeared`` — session is running on the machine but the
      control plane has no record of it. Admin edit path is the
      likely cause (someone deployed via ``mars deploy`` while the
      control plane was unreachable). Adopt the machine's state
      into the control plane.
    """

    in_sync: list[str] = field(default_factory=list)
    status_changes: list[ReconcileStatusChange] = field(default_factory=list)
    disappeared: list[KnownSessionState] = field(default_factory=list)
    appeared: list[MachineSessionState] = field(default_factory=list)

    @property
    def has_divergence(self) -> bool:
        return bool(
            self.status_changes or self.disappeared or self.appeared
        )

    @property
    def divergence_count(self) -> int:
        return (
            len(self.status_changes)
            + len(self.disappeared)
            + len(self.appeared)
        )


def reconcile_machine_state(
    *,
    known: Iterable[KnownSessionState],
    machine: Iterable[MachineSessionState],
) -> ReconcileReport:
    """Diff the control-plane view against the machine view.

    Pure function: no I/O, no mutation of inputs. Returns a fresh
    :class:`ReconcileReport`. Inputs are consumed exactly once so
    callers can pass generators.
    """
    known_map = {s.session_id: s for s in known}
    machine_map = {s.session_id: s for s in machine}

    report = ReconcileReport()

    for session_id, machine_state in machine_map.items():
        known_state = known_map.get(session_id)
        if known_state is None:
            report.appeared.append(machine_state)
            continue
        if known_state.status == machine_state.status:
            report.in_sync.append(session_id)
        else:
            report.status_changes.append(
                ReconcileStatusChange(
                    session_id=session_id,
                    previous=known_state.status,
                    current=machine_state.status,
                )
            )

    for session_id, known_state in known_map.items():
        if session_id not in machine_map:
            report.disappeared.append(known_state)

    return report
