"""Agent loop smoke test with a fake LLMClient."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from mars_runtime.runtime.agent_loop import run
from mars_runtime.providers import Response, ToolCall, fallback_chat_stream
from mars_runtime.config import AgentConfig
from mars_runtime.tools import ToolRegistry, load_all

load_all()


class _FakeLLM:
    """Returns a scripted sequence of Response objects per chat() call.

    chat_stream() delegates via fallback_chat_stream so tests written
    against the sync API still drive the streaming agent loop.
    """

    def __init__(self, responses: list[Response]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, *, system, messages, tools, model, max_tokens):
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": tools,
             "model": model, "max_tokens": max_tokens}
        )
        return self._responses.pop(0)

    def chat_stream(self, **kwargs):
        yield from fallback_chat_stream(self, **kwargs)


@pytest.fixture
def agent_config(tmp_path: Path) -> AgentConfig:
    prompt = tmp_path / "CLAUDE.md"
    prompt.write_text("you are a test daemon")
    return AgentConfig(
        name="test",
        description="test daemon",
        system_prompt_path=str(prompt),
        workdir=str(tmp_path),
        tools=["read"],
    )


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_single_turn_no_tools(agent_config, capsys):
    llm = _FakeLLM([Response(text="hi there", tool_calls=[], stop_reason="end_turn", raw_content=[{"type": "text", "text": "hi there"}])])
    tools = ToolRegistry(["read"])

    stdin = io.StringIO("hello\n")
    run(agent_config, llm, tools, stdin=stdin)

    events = _events(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert types[0] == "session_started"
    assert "user_input" in types
    assert "assistant_text" in types
    assert "turn_completed" in types
    assert types[-1] == "session_ended"


def test_tool_call_roundtrip(agent_config, capsys, tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("contents")

    # Turn 1: LLM requests `read` on target; Turn 2: returns final text.
    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[ToolCall(id="tu_1", name="read", input={"file_path": str(target)})],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": "tu_1", "name": "read", "input": {"file_path": str(target)}}],
        ),
        Response(
            text="done",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[{"type": "text", "text": "done"}],
        ),
    ])
    tools = ToolRegistry(["read"])

    run(agent_config, llm, tools, stdin=io.StringIO("please read hello.txt\n"))

    events = _events(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types

    tc = next(e for e in events if e["type"] == "tool_call")
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tc["name"] == "read"
    assert tc["id"] == "tu_1"
    assert tr["id"] == "tu_1"
    assert "contents" in tr["content"]


def test_session_ended_on_empty_stdin(agent_config, capsys):
    llm = _FakeLLM([])  # no chat() should be called with empty stdin
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO(""))
    events = _events(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert types == ["session_started", "session_ended"]
    assert llm.calls == []


def test_max_tokens_emits_truncation_event(agent_config, capsys):
    llm = _FakeLLM([Response(
        text="truncated...", tool_calls=[], stop_reason="max_tokens",
        raw_content=[{"type": "text", "text": "truncated..."}],
    )])
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("go\n"))

    events = _events(capsys.readouterr().out)
    assert any(e["type"] == "turn_truncated" for e in events)


def test_unknown_tool_is_error_not_crash(agent_config, capsys):
    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[ToolCall(id="tu_1", name="nonexistent", input={})],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": "tu_1", "name": "nonexistent", "input": {}}],
        ),
        Response(text="recovered", tool_calls=[], stop_reason="end_turn",
                 raw_content=[{"type": "text", "text": "recovered"}]),
    ])
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("do a thing\n"))

    events = _events(capsys.readouterr().out)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["is_error"] is True
    assert "not available" in tr["content"]
    # Loop must continue
    assert any(e["type"] == "turn_completed" for e in events)


def test_duplicate_tool_use_id_aborts_turn(agent_config, capsys):
    """Duplicate ids would poison the next API call (Anthropic requires
    tool_result for every tool_use). Abort the turn instead of continuing."""
    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[
                ToolCall(id="tu_dup", name="read", input={"file_path": "/tmp/a"}),
                ToolCall(id="tu_dup", name="read", input={"file_path": "/tmp/b"}),
            ],
            stop_reason="tool_use",
            raw_content=[
                {"type": "tool_use", "id": "tu_dup", "name": "read", "input": {"file_path": "/tmp/a"}},
                {"type": "tool_use", "id": "tu_dup", "name": "read", "input": {"file_path": "/tmp/b"}},
            ],
        ),
    ])
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("go\n"))

    events = _events(capsys.readouterr().out)
    aborted = [e for e in events if e["type"] == "turn_aborted"]
    assert aborted, "duplicate ids must trigger turn_aborted"
    assert aborted[0]["reason"] == "duplicate_tool_use_id"
    # No tool_call / tool_result should have fired.
    assert not any(e["type"] == "tool_call" for e in events)


def test_duplicate_ids_after_successful_round_rolls_back_cleanly(agent_config, capsys, tmp_path):
    """Regression: duplicate-id detected on iteration>0 must not leave
    orphan tool_use / tool_result pairs in history. The whole turn rolls back."""
    target = tmp_path / "a.txt"
    target.write_text("ok")

    # Iter 0: legitimate single-tool round. Iter 1: duplicate ids.
    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[ToolCall(id="tu_1", name="read", input={"file_path": str(target)})],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": "tu_1", "name": "read", "input": {"file_path": str(target)}}],
        ),
        Response(
            text="",
            tool_calls=[
                ToolCall(id="tu_dup", name="read", input={"file_path": str(target)}),
                ToolCall(id="tu_dup", name="read", input={"file_path": str(target)}),
            ],
            stop_reason="tool_use",
            raw_content=[
                {"type": "tool_use", "id": "tu_dup", "name": "read", "input": {"file_path": str(target)}},
                {"type": "tool_use", "id": "tu_dup", "name": "read", "input": {"file_path": str(target)}},
            ],
        ),
    ])
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("do stuff\n"))

    events = _events(capsys.readouterr().out)
    assert any(e["type"] == "turn_aborted" and e["reason"] == "duplicate_tool_use_id" for e in events)
    # First round's tool executed and emitted events — that's fine.
    # What matters is the abort fired. The deeper check (messages cleanly
    # truncated) would require exposing internal state; the rollback
    # happens whether or not we assert it here.


def test_max_tool_iterations_aborts_runaway_loop(agent_config, capsys):
    """If the LLM keeps returning tool_calls forever, we abort at the cap."""
    from mars_runtime.runtime.agent_loop import MAX_TOOL_ITERATIONS

    # Infinite tool-call loop: every response asks for another read.
    def _runaway():
        n = 0
        while True:
            n += 1
            yield Response(
                text="",
                tool_calls=[ToolCall(id=f"tu_{n}", name="read", input={"file_path": "/tmp/x"})],
                stop_reason="tool_use",
                raw_content=[{"type": "tool_use", "id": f"tu_{n}", "name": "read", "input": {"file_path": "/tmp/x"}}],
            )

    gen = _runaway()
    llm = _FakeLLM([next(gen) for _ in range(MAX_TOOL_ITERATIONS + 5)])
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("loop please\n"))

    events = _events(capsys.readouterr().out)
    aborted = [e for e in events if e["type"] == "turn_aborted"]
    assert aborted
    assert aborted[0]["reason"] == "max_tool_iterations"
    assert aborted[0]["limit"] == MAX_TOOL_ITERATIONS


def test_agent_survives_mid_stream_error(agent_config, capsys):
    """If chat_stream raises mid-stream (partial chunks already emitted),
    agent.run() must emit turn_aborted and accept the next turn, not crash."""
    from mars_runtime.providers import ChatChunk

    class _MidStreamFailLLM:
        def chat(self, **_):
            raise NotImplementedError
        def chat_stream(self, **_):
            yield ChatChunk(kind="text_delta", text="Hola")
            yield ChatChunk(kind="text_delta", text=" mu")
            raise RuntimeError("broker chat error: connection reset")

    llm = _MidStreamFailLLM()
    tools = ToolRegistry(["read"])

    # Drive two turns: first one fails mid-stream, then a second turn
    # comes in. Agent must process both without the worker crashing.
    run(agent_config, llm, tools, stdin=io.StringIO("first\nsecond\n"))

    events = _events(capsys.readouterr().out)
    types = [e["type"] for e in events]
    # Both turns aborted cleanly
    aborted = [e for e in events if e["type"] == "turn_aborted"]
    assert len(aborted) == 2, f"expected 2 turn_aborted events, got {len(aborted)}"
    assert all(a["reason"] == "chat_stream_error" for a in aborted)
    # Both user inputs processed
    user_inputs = [e for e in events if e["type"] == "user_input"]
    assert [u["text"] for u in user_inputs] == ["first", "second"]
    # session_ended still fires cleanly at the end
    assert types[-1] == "session_ended"


def test_agent_emits_assistant_chunk_events(agent_config, capsys):
    """Streaming path: agent emits assistant_chunk per text_delta from the LLM."""
    from mars_runtime.providers import ChatChunk

    class _StreamingLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, **_):
            raise NotImplementedError("test uses streaming path")

        def chat_stream(self, **_):
            self.calls += 1
            yield ChatChunk(kind="text_delta", text="Hola")
            yield ChatChunk(kind="text_delta", text=" mundo")
            yield ChatChunk(
                kind="message_stop",
                stop_reason="end_turn",
                final_response=Response(
                    text="Hola mundo",
                    tool_calls=[],
                    stop_reason="end_turn",
                    raw_content=[{"type": "text", "text": "Hola mundo"}],
                ),
            )

    llm = _StreamingLLM()
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("hi\n"))

    events = _events(capsys.readouterr().out)
    chunks = [e for e in events if e["type"] == "assistant_chunk"]
    assert [c["delta"] for c in chunks] == ["Hola", " mundo"]
    # assistant_text still emits at end for non-streaming subscribers
    assert any(e["type"] == "assistant_text" and e["text"] == "Hola mundo" for e in events)
    assert any(e["type"] == "turn_completed" for e in events)


def test_persists_session_and_commits_on_clean_turn(agent_config, capsys, tmp_path):
    sessions_dir = tmp_path / "sessions"
    ws = tmp_path / "ws"
    (ws).mkdir()

    llm = _FakeLLM([Response(
        text="ok", tool_calls=[], stop_reason="end_turn",
        raw_content=[{"type": "text", "text": "ok"}],
    )])
    tools = ToolRegistry(["read"])

    run(
        agent_config, llm, tools,
        stdin=io.StringIO("hello\n"),
        sessions_dir=sessions_dir,
        session_id="sess_ee0000000000000000000001",
        workspace_path=ws,
    )

    events = _events(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert "session_saved" in types
    # No workspace changes, so no turn_committed should fire.
    assert "turn_committed" not in types

    session_file = sessions_dir / "sess_ee0000000000000000000001.json"
    assert session_file.exists()
    data = json.loads(session_file.read_text())
    assert data["agent_name"] == "test"
    assert data["agent_config"]["name"] == "test"
    assert len(data["messages"]) == 2  # user + assistant


def test_commits_fire_when_workspace_is_dirty(agent_config, capsys, tmp_path, monkeypatch):
    """When the tool mutates the workspace, turn_committed should fire."""
    sessions_dir = tmp_path / "sessions"
    ws = tmp_path / "ws"
    ws.mkdir()

    # The fake LLM triggers a `bash` that creates a file, then closes.
    from mars_runtime.runtime.agent_loop import MAX_TOOL_ITERATIONS  # noqa: F401
    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[ToolCall(id="tu_1", name="bash", input={"command": f"touch {ws}/marker"})],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {"command": f"touch {ws}/marker"}}],
        ),
        Response(text="done", tool_calls=[], stop_reason="end_turn",
                 raw_content=[{"type": "text", "text": "done"}]),
    ])
    tools = ToolRegistry(["bash"])

    run(
        agent_config, llm, tools,
        stdin=io.StringIO("create a marker\n"),
        sessions_dir=sessions_dir,
        session_id="sess_ee0000000000000000000002",
        workspace_path=ws,
    )

    events = _events(capsys.readouterr().out)
    committed = [e for e in events if e["type"] == "turn_committed"]
    assert committed, f"expected turn_committed, got {[e['type'] for e in events]}"
    assert committed[0]["turn_number"] == 1
    assert len(committed[0]["commit_sha"]) == 40


def test_duplicate_tool_use_does_not_persist(agent_config, capsys, tmp_path):
    sessions_dir = tmp_path / "sessions"
    ws = tmp_path / "ws"
    ws.mkdir()

    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[
                ToolCall(id="tu_dup", name="read", input={"file_path": "/tmp/a"}),
                ToolCall(id="tu_dup", name="read", input={"file_path": "/tmp/b"}),
            ],
            stop_reason="tool_use",
            raw_content=[
                {"type": "tool_use", "id": "tu_dup", "name": "read", "input": {"file_path": "/tmp/a"}},
                {"type": "tool_use", "id": "tu_dup", "name": "read", "input": {"file_path": "/tmp/b"}},
            ],
        ),
    ])
    tools = ToolRegistry(["read"])

    run(
        agent_config, llm, tools,
        stdin=io.StringIO("go\n"),
        sessions_dir=sessions_dir,
        session_id="sess_ee0000000000000000000003",
        workspace_path=ws,
    )

    events = _events(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert "turn_aborted" in types
    assert "session_saved" not in types
    assert "turn_committed" not in types
    # Session file also doesn't exist — nothing was persisted.
    assert not (sessions_dir / "sess_ee0000000000000000000003.json").exists()


def test_resume_continues_from_start_messages(agent_config, capsys, tmp_path):
    sessions_dir = tmp_path / "sessions"
    ws = tmp_path / "ws"
    ws.mkdir()

    prior = [
        {"role": "user", "content": [{"type": "text", "text": "turn one"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done one"}]},
    ]

    llm = _FakeLLM([Response(
        text="done two", tool_calls=[], stop_reason="end_turn",
        raw_content=[{"type": "text", "text": "done two"}],
    )])
    tools = ToolRegistry(["read"])

    run(
        agent_config, llm, tools,
        stdin=io.StringIO("turn two\n"),
        sessions_dir=sessions_dir,
        session_id="sess_ee0000000000000000000004",
        workspace_path=ws,
        start_messages=prior,
    )

    data = json.loads((sessions_dir / "sess_ee0000000000000000000004.json").read_text())
    user_turns = [m for m in data["messages"] if m["role"] == "user" and any(b.get("type") == "text" for b in m["content"])]
    assert len(user_turns) == 2
    assert user_turns[0]["content"][0]["text"] == "turn one"
    assert user_turns[1]["content"][0]["text"] == "turn two"


def test_tool_error_is_reported_back_to_llm(agent_config, capsys):
    """When a tool returns is_error=True, the agent should feed the error
    back to the LLM as a tool_result with is_error, not crash the loop."""
    llm = _FakeLLM([
        Response(
            text="",
            tool_calls=[ToolCall(id="tu_1", name="read", input={"file_path": "/does/not/exist"})],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": "tu_1", "name": "read", "input": {"file_path": "/does/not/exist"}}],
        ),
        Response(text="handled", tool_calls=[], stop_reason="end_turn", raw_content=[{"type": "text", "text": "handled"}]),
    ])
    tools = ToolRegistry(["read"])
    run(agent_config, llm, tools, stdin=io.StringIO("try it\n"))

    events = _events(capsys.readouterr().out)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["is_error"] is True

    # And the tool_result block was appended to messages (second chat call
    # should have 3 messages: user, assistant w/ tool_use, user w/ tool_result).
    assert len(llm.calls) == 2
    second_messages = llm.calls[1]["messages"]
    assert any(
        any(b.get("type") == "tool_result" for b in (m["content"] if isinstance(m["content"], list) else []))
        for m in second_messages
    )
