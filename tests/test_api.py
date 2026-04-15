"""Public API surface — contract tests.

These tests verify that the mars_runtime.api module exposes a stable
surface for library consumers. They don't drive real sessions; those
are covered by test_cli.py and the integration smoke tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mars_runtime.api as api


def test_public_types_are_exported():
    # Types callers need for config + event handling
    assert api.AgentConfig is not None
    assert api.ChatChunk is not None
    assert api.Response is not None
    assert api.ToolCall is not None
    assert api.Message is not None
    assert api.ToolSpec is not None


def test_public_exceptions_are_exported():
    assert issubclass(api.BrokerDisconnected, Exception)
    assert issubclass(api.ProviderCollision, Exception)
    assert issubclass(api.InvalidSessionId, Exception)


def test_all_matches_module_attributes():
    """Every name in __all__ must resolve on the module (no typos)."""
    for name in api.__all__:
        assert hasattr(api, name), f"api.__all__ declares {name!r} but it's missing"


def test_list_sessions_empty_dir(tmp_path):
    empty = tmp_path / "data"
    assert api.list_sessions(data_dir=empty) == []


def test_list_sessions_reads_snapshots(tmp_path):
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    sid = "sess_" + "a" * 24
    (sessions_dir / f"{sid}.json").write_text(json.dumps({
        "id": sid,
        "agent_name": "my-agent",
        "created_at": 1,
        "messages": [],
    }))
    out = api.list_sessions(data_dir=data_dir)
    assert len(out) == 1
    assert out[0]["id"] == sid
    assert out[0]["agent_name"] == "my-agent"


def test_load_session_returns_dict(tmp_path):
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    sid = "sess_" + "b" * 24
    payload = {
        "id": sid,
        "agent_name": "tester",
        "agent_config": {"name": "tester", "description": "x", "system_prompt_path": "/tmp/x"},
        "created_at": 1,
        "messages": [],
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(payload))
    data = api.load_session(sid, data_dir=data_dir)
    assert data["agent_name"] == "tester"
    assert data["messages"] == []


def test_load_session_raises_on_invalid_id(tmp_path):
    with pytest.raises(api.InvalidSessionId):
        api.load_session("../evil", data_dir=tmp_path)


def test_resume_session_rejects_malformed_messages(tmp_path):
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    sid = "sess_" + "c" * 24
    (sessions_dir / f"{sid}.json").write_text(json.dumps({
        "id": sid,
        "agent_name": "t",
        "agent_config": {"name": "t", "description": "t", "system_prompt_path": "/tmp/x"},
        "created_at": 1,
        "messages": "not a list",
    }))
    with pytest.raises(ValueError, match="malformed messages"):
        api.resume_session(sid, data_dir=data_dir)


def test_api_does_not_leak_internals():
    """Library consumers should not see broker/worker internals by importing api."""
    for forbidden in ("broker", "worker", "_cli_run", "cli"):
        assert not hasattr(api, forbidden), (
            f"mars_runtime.api exposes {forbidden!r}; keep it internal"
        )
