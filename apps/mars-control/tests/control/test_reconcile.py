"""Unit tests for :mod:`mars_control.sessions.reconcile` — Story 5.4."""

from __future__ import annotations

import pytest

from mars_control.sessions.reconcile import (
    KnownSessionState,
    MachineSessionState,
    ReconcileReport,
    ReconcileStatusChange,
    reconcile_machine_state,
)


def _known(
    sid: str = "mars-s1",
    status: str = "running",
    agent: str = "pr-reviewer",
    url: str | None = "http://10.0.0.1:8080",
) -> KnownSessionState:
    return KnownSessionState(
        session_id=sid, agent_name=agent, status=status, supervisor_url=url
    )


def _machine(
    sid: str = "mars-s1",
    status: str = "running",
    agent: str = "pr-reviewer",
    url: str = "http://10.0.0.1:8080",
) -> MachineSessionState:
    return MachineSessionState(
        session_id=sid, agent_name=agent, status=status, supervisor_url=url
    )


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


def test_reconcile_empty_inputs_returns_empty_report():
    report = reconcile_machine_state(known=[], machine=[])
    assert report.in_sync == []
    assert report.status_changes == []
    assert report.disappeared == []
    assert report.appeared == []
    assert report.has_divergence is False
    assert report.divergence_count == 0


# ---------------------------------------------------------------------------
# In-sync case — no divergence
# ---------------------------------------------------------------------------


def test_both_sides_agree_session_in_sync():
    k = _known()
    m = _machine()
    report = reconcile_machine_state(known=[k], machine=[m])
    assert report.in_sync == ["mars-s1"]
    assert report.has_divergence is False


def test_multiple_in_sync_sessions():
    ks = [_known("mars-a"), _known("mars-b"), _known("mars-c")]
    ms = [_machine("mars-a"), _machine("mars-b"), _machine("mars-c")]
    report = reconcile_machine_state(known=ks, machine=ms)
    assert set(report.in_sync) == {"mars-a", "mars-b", "mars-c"}
    assert report.has_divergence is False


# ---------------------------------------------------------------------------
# status_changes — same session, different status
# ---------------------------------------------------------------------------


def test_status_change_recorded():
    report = reconcile_machine_state(
        known=[_known("mars-s1", status="running")],
        machine=[_machine("mars-s1", status="exited_clean")],
    )
    assert report.in_sync == []
    assert len(report.status_changes) == 1
    change = report.status_changes[0]
    assert change == ReconcileStatusChange(
        session_id="mars-s1", previous="running", current="exited_clean"
    )
    assert report.has_divergence is True


def test_status_change_does_not_pollute_other_buckets():
    report = reconcile_machine_state(
        known=[_known("mars-s1", status="running")],
        machine=[_machine("mars-s1", status="killed")],
    )
    assert report.disappeared == []
    assert report.appeared == []
    assert len(report.status_changes) == 1


# ---------------------------------------------------------------------------
# disappeared — known but not on machine
# ---------------------------------------------------------------------------


def test_disappeared_session_when_machine_empty():
    k = _known("mars-gone")
    report = reconcile_machine_state(known=[k], machine=[])
    assert report.disappeared == [k]
    assert report.divergence_count == 1


def test_disappeared_preserves_full_known_state():
    k = _known("mars-s1", status="running", url="http://10.0.0.1:8080")
    report = reconcile_machine_state(known=[k], machine=[])
    assert report.disappeared[0].supervisor_url == "http://10.0.0.1:8080"


# ---------------------------------------------------------------------------
# appeared — on machine but not in control plane
# ---------------------------------------------------------------------------


def test_appeared_session_when_known_empty():
    m = _machine("mars-new")
    report = reconcile_machine_state(known=[], machine=[m])
    assert report.appeared == [m]
    assert report.divergence_count == 1


def test_appeared_preserves_full_machine_state():
    m = _machine("mars-s1", status="running", url="http://10.0.0.1:8080")
    report = reconcile_machine_state(known=[], machine=[m])
    assert report.appeared[0].status == "running"
    assert report.appeared[0].supervisor_url == "http://10.0.0.1:8080"


# ---------------------------------------------------------------------------
# Mixed divergence
# ---------------------------------------------------------------------------


def test_mixed_reconcile_all_four_buckets():
    known = [
        _known("mars-sync", status="running"),  # in sync
        _known("mars-drift", status="running"),  # status change
        _known("mars-gone", status="running"),  # disappeared
    ]
    machine = [
        _machine("mars-sync", status="running"),  # in sync
        _machine("mars-drift", status="killed"),  # status change
        _machine("mars-new", status="running"),  # appeared
    ]
    report = reconcile_machine_state(known=known, machine=machine)

    assert report.in_sync == ["mars-sync"]
    assert len(report.status_changes) == 1
    assert report.status_changes[0].session_id == "mars-drift"
    assert [s.session_id for s in report.disappeared] == ["mars-gone"]
    assert [s.session_id for s in report.appeared] == ["mars-new"]
    assert report.divergence_count == 3
    assert report.has_divergence is True


# ---------------------------------------------------------------------------
# Properties + edge cases
# ---------------------------------------------------------------------------


def test_report_has_divergence_false_when_only_in_sync():
    report = reconcile_machine_state(
        known=[_known("s1")], machine=[_machine("s1")]
    )
    assert report.has_divergence is False
    assert report.divergence_count == 0


def test_report_has_divergence_true_on_any_bucket():
    # Disappeared only
    r1 = reconcile_machine_state(known=[_known("s1")], machine=[])
    assert r1.has_divergence is True
    # Appeared only
    r2 = reconcile_machine_state(known=[], machine=[_machine("s1")])
    assert r2.has_divergence is True
    # Status change only
    r3 = reconcile_machine_state(
        known=[_known("s1", status="running")],
        machine=[_machine("s1", status="killed")],
    )
    assert r3.has_divergence is True


def test_reconcile_accepts_generators():
    """Inputs are consumed once — callers can pass any Iterable."""
    report = reconcile_machine_state(
        known=(_known(f"s{i}") for i in range(3)),
        machine=(_machine(f"s{i}") for i in range(3)),
    )
    assert len(report.in_sync) == 3


def test_reconcile_does_not_mutate_inputs():
    known = [_known("s1")]
    machine = [_machine("s1")]
    reconcile_machine_state(known=known, machine=machine)
    # Lists unchanged (immutable dataclasses, just checking the containers)
    assert known == [_known("s1")]
    assert machine == [_machine("s1")]
