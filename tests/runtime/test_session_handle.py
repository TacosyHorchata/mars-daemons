"""Unit tests for :mod:`session.handle` — persistent session handles (Story 5.1)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from session.handle import (
    HANDLE_FILENAME,
    PersistedSessionHandle,
    atomic_write_handle,
    find_process_cmdline,
    is_claude_or_codex_process,
    is_pid_alive,
    read_handle,
    scan_workspace_handles,
)


def _make_handle(**overrides) -> PersistedSessionHandle:
    base = {
        "session_id": "mars-s-1",
        "agent_name": "pr-reviewer",
        "pid": 12345,
        "started_at": "2026-04-11T10:00:00+00:00",
        "last_heartbeat": "2026-04-11T10:05:00+00:00",
        "agent_yaml_path": "/workspace/pr-reviewer/agent.yaml",
        "workdir": "/workspace/mars-s-1",
        "metadata": {},
    }
    base.update(overrides)
    return PersistedSessionHandle(**base)


# ---------------------------------------------------------------------------
# Serde
# ---------------------------------------------------------------------------


def test_handle_to_and_from_dict_round_trips():
    handle = _make_handle(metadata={"region": "iad"})
    data = handle.to_dict()
    restored = PersistedSessionHandle.from_dict(data)
    assert restored == handle


def test_handle_from_dict_tolerates_unknown_fields():
    data = {
        "session_id": "mars-s-1",
        "agent_name": "pr",
        "pid": 42,
        "started_at": "2026-04-11T10:00:00+00:00",
        "last_heartbeat": "2026-04-11T10:05:00+00:00",
        "agent_yaml_path": "/w/agent.yaml",
        "workdir": "/w",
        "future_field": "v2",  # unknown — should be ignored
    }
    handle = PersistedSessionHandle.from_dict(data)
    assert handle.session_id == "mars-s-1"


def test_handle_from_dict_rejects_missing_required_field():
    with pytest.raises(KeyError):
        PersistedSessionHandle.from_dict({"session_id": "x"})


def test_with_heartbeat_updates_only_heartbeat_field():
    handle = _make_handle()
    bumped = handle.with_heartbeat(datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc))
    assert bumped.last_heartbeat == "2026-04-11T12:00:00+00:00"
    assert bumped.session_id == handle.session_id
    assert bumped.pid == handle.pid
    assert bumped.started_at == handle.started_at


# ---------------------------------------------------------------------------
# Atomic write + read
# ---------------------------------------------------------------------------


def test_atomic_write_creates_handle_file_with_payload(tmp_path: Path):
    handle = _make_handle()
    path = atomic_write_handle(handle, tmp_path)
    assert path == tmp_path / HANDLE_FILENAME
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == handle.session_id
    assert data["pid"] == handle.pid


def test_atomic_write_creates_parent_dir_if_missing(tmp_path: Path):
    subdir = tmp_path / "nested" / "session"
    handle = _make_handle()
    path = atomic_write_handle(handle, subdir)
    assert path.exists()


def test_atomic_write_removes_tmp_file(tmp_path: Path):
    handle = _make_handle()
    atomic_write_handle(handle, tmp_path)
    tmp = tmp_path / f"{HANDLE_FILENAME}.tmp"
    assert not tmp.exists()


def test_atomic_write_overwrites_existing_handle(tmp_path: Path):
    handle_v1 = _make_handle(pid=111)
    handle_v2 = _make_handle(pid=222)
    atomic_write_handle(handle_v1, tmp_path)
    atomic_write_handle(handle_v2, tmp_path)
    loaded = read_handle(tmp_path)
    assert loaded is not None
    assert loaded.pid == 222


def test_atomic_write_sets_0600_permissions(tmp_path: Path):
    atomic_write_handle(_make_handle(), tmp_path)
    path = tmp_path / HANDLE_FILENAME
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_read_handle_returns_none_when_file_missing(tmp_path: Path):
    assert read_handle(tmp_path) is None


def test_read_handle_returns_none_on_malformed_json(tmp_path: Path):
    path = tmp_path / HANDLE_FILENAME
    path.write_text("{not json}")
    assert read_handle(tmp_path) is None


def test_read_handle_returns_none_on_non_dict_payload(tmp_path: Path):
    path = tmp_path / HANDLE_FILENAME
    path.write_text("[1, 2, 3]")
    assert read_handle(tmp_path) is None


def test_read_handle_returns_none_on_missing_required_field(tmp_path: Path):
    path = tmp_path / HANDLE_FILENAME
    path.write_text(json.dumps({"session_id": "incomplete"}))
    assert read_handle(tmp_path) is None


def test_read_handle_roundtrip(tmp_path: Path):
    original = _make_handle(metadata={"x": 1})
    atomic_write_handle(original, tmp_path)
    loaded = read_handle(tmp_path)
    assert loaded == original


# ---------------------------------------------------------------------------
# PID liveness check
# ---------------------------------------------------------------------------


def test_is_pid_alive_true_for_current_process():
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_false_for_dead_pid():
    """Find a pid that doesn't exist — PIDs in the 2^20+ range are
    unlikely to exist on a dev machine."""
    assert is_pid_alive(9_999_999) is False


def test_is_pid_alive_false_for_zero_or_negative():
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False


# ---------------------------------------------------------------------------
# Cmdline check
# ---------------------------------------------------------------------------


def test_find_process_cmdline_returns_something_for_self():
    cmdline = find_process_cmdline(os.getpid())
    assert cmdline  # non-empty
    # Should at least mention python or pytest in the args
    lowered = cmdline.lower()
    assert "python" in lowered or "pytest" in lowered


def test_find_process_cmdline_empty_for_dead_pid():
    assert find_process_cmdline(9_999_999) == ""


def test_is_claude_or_codex_process_false_for_self():
    """The pytest process is neither claude nor codex."""
    assert is_claude_or_codex_process(os.getpid()) is False


def test_is_claude_or_codex_process_false_for_dead_pid():
    assert is_claude_or_codex_process(9_999_999) is False


def test_is_claude_or_codex_process_token_match(monkeypatch):
    """Feed a fake cmdline that contains 'claude' as a token."""

    def _fake_cmdline(pid: int) -> str:
        return "/usr/bin/node /usr/local/lib/node_modules/claude --print hi"

    import session.handle as handle_mod

    monkeypatch.setattr(handle_mod, "find_process_cmdline", _fake_cmdline)
    assert handle_mod.is_claude_or_codex_process(12345) is True


def test_is_claude_or_codex_process_rejects_substring_only(monkeypatch):
    """'claudetronic' and 'codexplorer' must NOT match — we require
    an exact token match so PID reuse catches misleading names."""

    def _fake_cmdline(pid: int) -> str:
        return "/usr/bin/node /usr/local/lib/claudetronic --run"

    import session.handle as handle_mod

    monkeypatch.setattr(handle_mod, "find_process_cmdline", _fake_cmdline)
    assert handle_mod.is_claude_or_codex_process(12345) is False


def test_is_claude_or_codex_process_codex_token(monkeypatch):
    def _fake_cmdline(pid: int) -> str:
        return "node /usr/local/bin/codex exec --json -"

    import session.handle as handle_mod

    monkeypatch.setattr(handle_mod, "find_process_cmdline", _fake_cmdline)
    assert handle_mod.is_claude_or_codex_process(12345) is True


def test_is_claude_or_codex_process_empty_cmdline(monkeypatch):
    def _fake_cmdline(pid: int) -> str:
        return ""

    import session.handle as handle_mod

    monkeypatch.setattr(handle_mod, "find_process_cmdline", _fake_cmdline)
    assert handle_mod.is_claude_or_codex_process(12345) is False


# ---------------------------------------------------------------------------
# Workspace scan
# ---------------------------------------------------------------------------


def test_scan_workspace_returns_empty_for_missing_root(tmp_path: Path):
    result = scan_workspace_handles(tmp_path / "nonexistent")
    assert result == []


def test_scan_workspace_skips_dirs_without_handle(tmp_path: Path):
    (tmp_path / "empty-session").mkdir()
    assert scan_workspace_handles(tmp_path) == []


def test_scan_workspace_loads_valid_handles(tmp_path: Path):
    session_a = tmp_path / "mars-a"
    session_a.mkdir()
    session_b = tmp_path / "mars-b"
    session_b.mkdir()
    atomic_write_handle(_make_handle(session_id="mars-a"), session_a)
    atomic_write_handle(_make_handle(session_id="mars-b"), session_b)

    result = scan_workspace_handles(tmp_path)
    assert len(result) == 2
    ids = {handle.session_id for _, handle in result if handle is not None}
    assert ids == {"mars-a", "mars-b"}


def test_scan_workspace_returns_none_for_corrupt_handle(tmp_path: Path):
    sess = tmp_path / "mars-bad"
    sess.mkdir()
    (sess / HANDLE_FILENAME).write_text("{not json}")
    result = scan_workspace_handles(tmp_path)
    assert len(result) == 1
    _, handle = result[0]
    assert handle is None


def test_scan_workspace_sorted_by_directory_name(tmp_path: Path):
    for name in ("mars-c", "mars-a", "mars-b"):
        d = tmp_path / name
        d.mkdir()
        atomic_write_handle(_make_handle(session_id=name), d)
    result = scan_workspace_handles(tmp_path)
    names = [p.name for p, _ in result]
    assert names == ["mars-a", "mars-b", "mars-c"]
