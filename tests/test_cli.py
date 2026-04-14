"""CLI tests. Stubs the LLM provider factory so no network calls happen."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mars_runtime import __main__ as cli
from mars_runtime.llm_client import Response


class _StubLLM:
    def __init__(self, reply: str = "hello back"):
        self._reply = reply

    def chat(self, *, system, messages, tools, model, max_tokens):
        return Response(
            text=self._reply,
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[{"type": "text", "text": self._reply}],
        )


@pytest.fixture
def yaml_and_prompt(tmp_path: Path) -> Path:
    """Write a valid agent.yaml with a sibling CLAUDE.md."""
    (tmp_path / "CLAUDE.md").write_text("you are a test daemon")
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        "name: test-agent\n"
        "description: test agent\n"
        "model: claude-opus-4-5\n"
        "system_prompt_path: ./CLAUDE.md\n"
        "tools: [read]\n"
    )
    return yaml_path


def test_new_session_creates_snapshot_and_commits(yaml_and_prompt, tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("hi\n"))

    with patch("mars_runtime.__main__.llm_client.get", return_value=_StubLLM()):
        rc = cli.main([str(yaml_and_prompt)])

    assert rc == 0
    sessions = list((data_dir / "sessions").glob("sess_*.json"))
    assert len(sessions) == 1
    data = json.loads(sessions[0].read_text())
    assert data["agent_name"] == "test-agent"
    assert data["messages"]
    assert (data_dir / "workspace" / ".git").is_dir()


def test_list_shows_recent_sessions(yaml_and_prompt, tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("hi\n"))

    with patch("mars_runtime.__main__.llm_client.get", return_value=_StubLLM()):
        cli.main([str(yaml_and_prompt)])

    capsys.readouterr()  # drain first session's events

    rc = cli.main(["--list"])
    assert rc == 0
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    entry = json.loads(output[0])
    assert entry["agent_name"] == "test-agent"
    assert entry["id"].startswith("sess_")


def test_resume_continues_prior_messages(yaml_and_prompt, tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))

    # Turn 1.
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("first\n"))
    with patch("mars_runtime.__main__.llm_client.get", return_value=_StubLLM("reply1")):
        cli.main([str(yaml_and_prompt)])

    sessions = list((data_dir / "sessions").glob("sess_*.json"))
    session_id = sessions[0].stem
    capsys.readouterr()

    # Turn 2 via --resume.
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("second\n"))
    with patch("mars_runtime.__main__.llm_client.get", return_value=_StubLLM("reply2")):
        rc = cli.main(["--resume", session_id])
    assert rc == 0

    data = json.loads(sessions[0].read_text())
    user_turns = [
        m for m in data["messages"]
        if m["role"] == "user" and any(b.get("type") == "text" for b in m["content"])
    ]
    assert [t["content"][0]["text"] for t in user_turns] == ["first", "second"]


def test_missing_session_id_returns_error(tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = cli.main(["--resume", "sess_deadbeef0000000000000000"])
    assert rc == 1
    assert "session not found" in capsys.readouterr().err


def test_resume_rejects_invalid_session_id(tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = cli.main(["--resume", "../../../etc/passwd"])
    assert rc == 2
    assert "invalid session id" in capsys.readouterr().err


def test_resume_rejects_path_traversal(tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = cli.main(["--resume", "sess_../secret"])
    assert rc == 2


def test_git_subprocess_error_is_caught(yaml_and_prompt, tmp_path, capsys, monkeypatch):
    """CalledProcessError from git must not surface as a traceback.

    Git runs in the worker process, so this test invokes the worker
    directly rather than spawning a subprocess (which can't inherit
    monkeypatches).
    """
    import io
    import json
    import subprocess
    from mars_runtime import _worker
    from mars_runtime.llm_client import Response

    data_dir = tmp_path / "data"
    (data_dir / "workspace").mkdir(parents=True)
    (data_dir / "sessions").mkdir(parents=True)
    from mars_runtime.schema import AgentConfig
    config = AgentConfig.from_yaml_file(yaml_and_prompt)
    config = config.model_copy(update={"system_prompt_path": str((yaml_and_prompt.parent / "CLAUDE.md").resolve())})

    def _broken_commit(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, ["git", "commit"], output="", stderr="denied")

    # Worker reads RPC from stdin, writes to stdout. Feed it a user_input
    # + eof + chat_response. The chat_response must arrive AFTER the
    # worker emits its chat_request, but since the user_input triggers
    # turn processing which issues chat_request, we can prime the pipe
    # in order because json-line reads are synchronous.
    stub_response = {
        "rpc": "chat_response",
        "id": 0,
        "response": {
            "text": "ok",
            "tool_calls": [],
            "stop_reason": "end_turn",
            "raw_content": [{"type": "text", "text": "ok"}],
        },
    }
    stdin_script = (
        json.dumps({"rpc": "user_input", "text": "hi"}) + "\n"
        + json.dumps(stub_response) + "\n"
        + json.dumps({"rpc": "eof"}) + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_script))
    monkeypatch.setattr("mars_runtime.workspace.commit_turn", _broken_commit)

    rc = _worker.main([
        "--agent-json", json.dumps(config.model_dump()),
        "--session-id", "sess_cc0000000000000000000099",
        "--data-dir", str(data_dir),
    ])
    assert rc == 1
    assert "git error" in capsys.readouterr().err


def _write_resume_fixture(sessions: Path, sid: str, messages: object) -> None:
    import json as _json
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.json").write_text(
        _json.dumps({
            "id": sid,
            "agent_name": "weird",
            "agent_config": {"name": "weird", "description": "x", "system_prompt_path": "/tmp/x"},
            "created_at": 1,
            "messages": messages,
        })
    )


def test_resume_rejects_malformed_messages_top_level(tmp_path, capsys, monkeypatch):
    data_dir = tmp_path / "data"
    sid = "sess_dd0000000000000000000001"
    _write_resume_fixture(data_dir / "sessions", sid, "not a list")
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = cli.main(["--resume", sid])
    assert rc == 1
    assert "malformed messages" in capsys.readouterr().err


def test_resume_rejects_malformed_content_blocks(tmp_path, capsys, monkeypatch):
    """content=[1] passes shallow validation but would crash the agent loop."""
    data_dir = tmp_path / "data"
    sid = "sess_dd0000000000000000000002"
    _write_resume_fixture(
        data_dir / "sessions", sid,
        [{"role": "user", "content": [1, 2, 3]}],
    )
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = cli.main(["--resume", sid])
    assert rc == 1
    assert "malformed messages" in capsys.readouterr().err


def test_yaml_and_resume_are_mutually_exclusive(yaml_and_prompt, capsys):
    with pytest.raises(SystemExit):
        cli.main([str(yaml_and_prompt), "--resume", "sess_abc"])
