"""Story 5.3 — supervisor startup recovery scan."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from session.handle import (
    HANDLE_FILENAME,
    PersistedSessionHandle,
    atomic_write_handle,
)
from supervisor_recovery import (
    RecoveredSession,
    RecoveryStatus,
    classify_session,
    recover_workspace,
)


def _make_handle(
    *,
    session_id: str = "mars-sess-1",
    pid: int = 12345,
    agent_name: str = "pr-reviewer",
) -> PersistedSessionHandle:
    return PersistedSessionHandle(
        session_id=session_id,
        agent_name=agent_name,
        pid=pid,
        started_at="2026-04-11T10:00:00+00:00",
        last_heartbeat="2026-04-11T10:05:00+00:00",
        agent_yaml_path=f"/workspace/{agent_name}/agent.yaml",
        workdir=f"/workspace/{session_id}",
    )


# ---------------------------------------------------------------------------
# classify_session — the pure core
# ---------------------------------------------------------------------------


def test_classify_none_handle_returns_corrupt(tmp_path: Path):
    result = classify_session(tmp_path / "sess", None)
    assert result.status == "corrupt_handle"
    assert result.handle is None
    assert "missing or unparseable" in result.reason
    assert result.needs_restart is True


def test_classify_dead_pid_returns_dead(tmp_path: Path):
    handle = _make_handle(pid=9999)
    result = classify_session(
        tmp_path / "sess",
        handle,
        is_alive=lambda pid: False,
        is_claude_or_codex=lambda pid: False,
    )
    assert result.status == "dead"
    assert result.handle == handle
    assert "9999" in result.reason
    assert "not running" in result.reason


def test_classify_alive_non_claude_returns_orphan_alive(tmp_path: Path):
    handle = _make_handle(pid=5555)
    result = classify_session(
        tmp_path / "sess",
        handle,
        is_alive=lambda pid: True,
        is_claude_or_codex=lambda pid: False,
    )
    assert result.status == "orphan_alive"
    assert "PID reuse" in result.reason


def test_classify_alive_claude_returns_reattach_candidate(tmp_path: Path):
    handle = _make_handle(pid=7777)
    result = classify_session(
        tmp_path / "sess",
        handle,
        is_alive=lambda pid: True,
        is_claude_or_codex=lambda pid: True,
    )
    assert result.status == "reattach_candidate"
    assert "admin must decide" in result.reason
    # Even "still running" triggers needs_restart — v1 does not
    # auto-reattach.
    assert result.needs_restart is True


def test_all_statuses_map_to_needs_restart(tmp_path: Path):
    """Every recovery outcome should flag needs_restart=True. The
    control plane UI uses that as the sole gate for 'show Resume
    button'."""
    statuses: list[RecoveryStatus] = [
        "dead",
        "orphan_alive",
        "reattach_candidate",
        "corrupt_handle",
    ]
    for status in statuses:
        session = RecoveredSession(
            session_dir=tmp_path / "s",
            status=status,
            handle=None,
            reason="x",
        )
        assert session.needs_restart is True


def test_session_id_property_extracts_from_handle(tmp_path: Path):
    handle = _make_handle(session_id="mars-xyz")
    result = classify_session(
        tmp_path / "sess",
        handle,
        is_alive=lambda pid: False,
        is_claude_or_codex=lambda pid: False,
    )
    assert result.session_id == "mars-xyz"


def test_session_id_property_none_for_corrupt_handle(tmp_path: Path):
    result = classify_session(tmp_path / "sess", None)
    assert result.session_id is None


# ---------------------------------------------------------------------------
# recover_workspace — integrates scan + classify
# ---------------------------------------------------------------------------


def test_recover_workspace_empty_root_returns_empty(tmp_path: Path):
    assert recover_workspace(tmp_path / "nonexistent") == []


def test_recover_workspace_ignores_dirs_without_handle(tmp_path: Path):
    (tmp_path / "sess-no-handle").mkdir()
    assert recover_workspace(tmp_path) == []


def test_recover_workspace_marks_all_dead_when_no_pids_alive(tmp_path: Path):
    # 3 sessions, all with dead pids
    for sid in ("mars-a", "mars-b", "mars-c"):
        d = tmp_path / sid
        d.mkdir()
        atomic_write_handle(_make_handle(session_id=sid, pid=9_999_999), d)

    results = recover_workspace(
        tmp_path,
        is_alive=lambda pid: False,
        is_claude_or_codex=lambda pid: False,
    )
    assert len(results) == 3
    assert all(r.status == "dead" for r in results)
    # Sorted by dir name
    assert [r.session_id for r in results] == ["mars-a", "mars-b", "mars-c"]


def test_recover_workspace_mixed_outcomes(tmp_path: Path):
    """Simulate a crash where:
    - mars-a: dead pid
    - mars-b: alive pid, unrelated process (orphan)
    - mars-c: alive claude pid (reattach candidate)
    - mars-d: corrupt handle
    """
    for sid, pid in (("mars-a", 1001), ("mars-b", 1002), ("mars-c", 1003)):
        d = tmp_path / sid
        d.mkdir()
        atomic_write_handle(_make_handle(session_id=sid, pid=pid), d)

    # Corrupt handle for mars-d
    d = tmp_path / "mars-d"
    d.mkdir()
    (d / HANDLE_FILENAME).write_text("{corrupt")

    def _fake_alive(pid: int) -> bool:
        return pid in (1002, 1003)

    def _fake_claude(pid: int) -> bool:
        return pid == 1003

    results = recover_workspace(
        tmp_path,
        is_alive=_fake_alive,
        is_claude_or_codex=_fake_claude,
    )

    by_sid = {r.session_id or r.session_dir.name: r for r in results}
    assert by_sid["mars-a"].status == "dead"
    assert by_sid["mars-b"].status == "orphan_alive"
    assert by_sid["mars-c"].status == "reattach_candidate"
    # Corrupt handle: session_id is None so key is dir name
    assert by_sid["mars-d"].status == "corrupt_handle"


def test_recover_workspace_logs_warnings_for_alive_pids(tmp_path: Path, caplog):
    d = tmp_path / "mars-live"
    d.mkdir()
    atomic_write_handle(_make_handle(session_id="mars-live", pid=1234), d)

    with caplog.at_level("WARNING", logger="mars.runtime.recovery"):
        recover_workspace(
            tmp_path,
            is_alive=lambda pid: True,
            is_claude_or_codex=lambda pid: True,
        )
    assert any("still running" in r.message for r in caplog.records)


def test_recover_workspace_logs_info_for_dead(tmp_path: Path, caplog):
    d = tmp_path / "mars-dead"
    d.mkdir()
    atomic_write_handle(
        _make_handle(session_id="mars-dead", pid=999999), d
    )

    with caplog.at_level("INFO", logger="mars.runtime.recovery"):
        recover_workspace(
            tmp_path,
            is_alive=lambda pid: False,
            is_claude_or_codex=lambda pid: False,
        )
    assert any("is dead" in r.message for r in caplog.records)


def test_recover_workspace_logs_warning_for_orphan(tmp_path: Path, caplog):
    d = tmp_path / "mars-orphan"
    d.mkdir()
    atomic_write_handle(
        _make_handle(session_id="mars-orphan", pid=1111), d
    )

    with caplog.at_level("WARNING", logger="mars.runtime.recovery"):
        recover_workspace(
            tmp_path,
            is_alive=lambda pid: True,
            is_claude_or_codex=lambda pid: False,
        )
    assert any(
        "PID reuse suspected" in r.message for r in caplog.records
    )


def test_recover_workspace_logs_warning_for_corrupt(tmp_path: Path, caplog):
    d = tmp_path / "mars-corrupt"
    d.mkdir()
    (d / HANDLE_FILENAME).write_text("{garbage")

    with caplog.at_level("WARNING", logger="mars.runtime.recovery"):
        recover_workspace(
            tmp_path,
            is_alive=lambda pid: True,
            is_claude_or_codex=lambda pid: True,
        )
    assert any(
        "corrupt handle file" in r.message for r in caplog.records
    )
