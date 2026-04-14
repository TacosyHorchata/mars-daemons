"""JSON-per-session persistence tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

from mars_runtime import session_store


def test_new_id_is_prefixed_and_unique():
    a = session_store.new_id()
    b = session_store.new_id()
    assert a.startswith("sess_")
    assert a != b


def test_save_and_load_preserves_messages_exactly(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    session_id = "sess_0123456789abcdef01234567"
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "reading a file"},
                {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"file_path": "/x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "contents", "is_error": False}
            ],
        },
    ]

    session_store.save(
        sessions_dir,
        session_id,
        agent_name="pr-reviewer",
        agent_config={"name": "pr-reviewer", "model": "claude-opus-4-5"},
        messages=messages,
    )

    loaded = session_store.load(sessions_dir, session_id)
    assert loaded["id"] == session_id
    assert loaded["agent_name"] == "pr-reviewer"
    assert loaded["agent_config"] == {"name": "pr-reviewer", "model": "claude-opus-4-5"}
    assert loaded["messages"] == messages
    assert isinstance(loaded["created_at"], int)


def test_save_is_atomic_no_tmp_leaks(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    session_id = "sess_a10000000000000000000000"

    session_store.save(sessions_dir, session_id, "a", {}, [{"role": "user", "content": []}])

    session_files = list(sessions_dir.iterdir())
    assert len(session_files) == 1
    assert session_files[0].name == f"{session_id}.json"
    # No leftover .tmp siblings.
    assert not list(sessions_dir.glob("*.tmp"))


def test_save_preserves_created_at_across_turns(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    session_id = "sess_b20000000000000000000000"

    session_store.save(sessions_dir, session_id, "a", {}, [], created_at=1000)
    time.sleep(0.01)
    session_store.save(sessions_dir, session_id, "a", {}, [{"role": "user", "content": []}])

    loaded = session_store.load(sessions_dir, session_id)
    assert loaded["created_at"] == 1000


def test_list_recent_newest_first(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"

    ids = [
        "sess_000000000000000000000000",
        "sess_000000000000000000000001",
        "sess_000000000000000000000002",
    ]
    for i, sid in enumerate(ids):
        session_store.save(sessions_dir, sid, f"agent{i}", {}, [])
        time.sleep(0.01)

    recent = session_store.list_recent(sessions_dir)
    assert [r["agent_name"] for r in recent] == ["agent2", "agent1", "agent0"]


def test_list_recent_respects_limit(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    for i in range(5):
        session_store.save(sessions_dir, f"sess_{i:024d}", f"a{i}", {}, [])
        time.sleep(0.005)

    assert len(session_store.list_recent(sessions_dir, limit=2)) == 2


def test_list_recent_skips_malformed_json(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    good_id = "sess_c30000000000000000000000"
    bad_id = "sess_c40000000000000000000000"
    (sessions_dir / f"{good_id}.json").write_text(
        json.dumps({"id": good_id, "agent_name": "ok", "created_at": 1, "messages": []})
    )
    (sessions_dir / f"{bad_id}.json").write_text("not valid json{{")

    recent = session_store.list_recent(sessions_dir)
    assert len(recent) == 1
    assert recent[0]["agent_name"] == "ok"


def test_list_recent_on_missing_dir_returns_empty(tmp_path: Path):
    assert session_store.list_recent(tmp_path / "does-not-exist") == []


def test_save_rejects_invalid_session_id(tmp_path: Path):
    import pytest

    with pytest.raises(session_store.InvalidSessionId):
        session_store.save(tmp_path, "../evil", "a", {}, [])
    with pytest.raises(session_store.InvalidSessionId):
        session_store.save(tmp_path, "sess_short", "a", {}, [])


def test_load_rejects_path_traversal(tmp_path: Path):
    import pytest

    with pytest.raises(session_store.InvalidSessionId):
        session_store.load(tmp_path, "../../etc/passwd")


def test_list_recent_ignores_non_sess_files(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Attacker-planted file with valid JSON but wrong name.
    (sessions_dir / "arbitrary.json").write_text(
        json.dumps({"id": "sess_fake", "agent_name": "evil", "created_at": 1, "messages": []})
    )
    # File with sess_ prefix but invalid id shape.
    (sessions_dir / "sess_short.json").write_text(
        json.dumps({"id": "sess_short", "agent_name": "evil", "created_at": 1, "messages": []})
    )

    assert session_store.list_recent(sessions_dir) == []


def test_new_id_format():
    import re
    sid = session_store.new_id()
    assert re.fullmatch(r"sess_[0-9a-f]{24}", sid)


def test_list_recent_rejects_id_mismatch(tmp_path: Path):
    """A planted file with valid filename but forged in-file id is rejected."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    good_id = "sess_aa0000000000000000000001"
    forged_id = "sess_bb9999999999999999999999"  # claims to be a different session
    # Filename is valid, but data["id"] disagrees.
    (sessions_dir / f"{good_id}.json").write_text(
        json.dumps({"id": forged_id, "agent_name": "planted", "created_at": 1, "messages": []})
    )
    assert session_store.list_recent(sessions_dir) == []


def test_list_recent_survives_malformed_messages(tmp_path: Path):
    """Regression: _count_user_turns must not crash on non-list messages."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sid = "sess_cc0000000000000000000001"
    (sessions_dir / f"{sid}.json").write_text(
        json.dumps({
            "id": sid,
            "agent_name": "weird",
            "created_at": 1,
            "messages": {"not": "a list"},
        })
    )
    out = session_store.list_recent(sessions_dir)
    assert len(out) == 1
    assert out[0]["turn_count"] == 0


def test_is_valid_messages_shape():
    assert session_store._is_valid_messages_shape([])
    assert session_store._is_valid_messages_shape(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    assert session_store._is_valid_messages_shape(
        [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reply"},
                {"type": "tool_use", "id": "tu_1", "name": "read", "input": {}},
            ]},
        ]
    )
    # Malformed top-level cases:
    assert not session_store._is_valid_messages_shape("string")
    assert not session_store._is_valid_messages_shape({"not": "list"})
    # Malformed role:
    assert not session_store._is_valid_messages_shape([{"role": "system", "content": []}])
    assert not session_store._is_valid_messages_shape([{"content": []}])  # no role
    # Malformed content:
    assert not session_store._is_valid_messages_shape([{"role": "user", "content": "str"}])
    # Malformed content BLOCK types (what codex caught):
    assert not session_store._is_valid_messages_shape([{"role": "user", "content": [1]}])
    assert not session_store._is_valid_messages_shape([{"role": "user", "content": [None]}])
    assert not session_store._is_valid_messages_shape(
        [{"role": "user", "content": [{"no_type": "here"}]}]
    )
    assert not session_store._is_valid_messages_shape(
        [{"role": "user", "content": [{"type": 42}]}]  # type not a string
    )
