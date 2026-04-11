"""Unit tests for ``mars run --local`` (Story 6.1).

Exercises the pure-function helpers directly, then drives the
:func:`run_local_loop` with a fake spawn function that writes a
canonical stream-json sequence to its stdout and captures what the
prompt loop writes to its stdin.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from events.types import (
    AssistantChunk,
    AssistantText,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
)
from mars.runtime_local import (
    encode_user_event_line,
    format_event_for_terminal,
    read_multiline_prompt,
    run_local_loop,
)
from schema.agent import AgentConfig


def _config() -> AgentConfig:
    return AgentConfig(
        name="local-test",
        description="local-mode test agent",
        runtime="claude-code",
        system_prompt_path="CLAUDE.md",
        workdir="/tmp",
    )


# ---------------------------------------------------------------------------
# Pure helpers — format_event_for_terminal + encode_user_event_line
# ---------------------------------------------------------------------------


def test_format_session_started_has_model_and_version():
    ev = SessionStarted(
        session_id="s-1",
        model="claude-opus-4-6",
        cwd="/w",
        claude_code_version="2.1.101",
    )
    line = format_event_for_terminal(ev)
    assert "session started" in line
    assert "claude-opus-4-6" in line
    assert "2.1.101" in line


def test_format_assistant_text_starts_with_left_arrow():
    ev = AssistantText(session_id="s-1", text="hello there")
    line = format_event_for_terminal(ev)
    assert line.startswith("← ")
    assert "hello there" in line


def test_format_assistant_chunk_uses_dot_prefix():
    ev = AssistantChunk(session_id="s-1", delta="he")
    line = format_event_for_terminal(ev)
    assert line.startswith(".. ")
    assert "he" in line


def test_format_tool_call_includes_name_and_input():
    ev = ToolCall(
        session_id="s-1",
        tool_use_id="tu-1",
        tool_name="Bash",
        input={"command": "echo hi"},
    )
    line = format_event_for_terminal(ev)
    assert "tool_call: Bash" in line
    assert "echo hi" in line


def test_format_tool_result_handles_error_flag():
    ev = ToolResult(session_id="s-1", tool_use_id="tu-1", content="boom", is_error=True)
    line = format_event_for_terminal(ev)
    assert "(error)" in line
    assert "boom" in line


def test_format_tool_result_truncates_long_content():
    ev = ToolResult(
        session_id="s-1",
        tool_use_id="tu-1",
        content="a" * 500,
    )
    line = format_event_for_terminal(ev)
    # 200 char budget + prefix
    assert len([c for c in line if c == "a"]) <= 220


def test_format_session_ended_includes_cost_and_turns():
    ev = SessionEnded(
        session_id="s-1",
        result="done",
        stop_reason="end_turn",
        duration_ms=1000,
        num_turns=3,
        total_cost_usd=0.0123,
    )
    line = format_event_for_terminal(ev)
    assert "session ended" in line
    assert "end_turn" in line
    assert "$0.0123" in line
    assert "turns=3" in line


def test_encode_user_event_line_produces_valid_stream_json():
    line = encode_user_event_line("hello world")
    text = line.decode("utf-8").rstrip("\n")
    parsed = json.loads(text)
    assert parsed["type"] == "user"
    content = parsed["message"]["content"]
    assert content[0] == {"type": "text", "text": "hello world"}


def test_encode_user_event_line_terminates_with_newline():
    line = encode_user_event_line("x")
    assert line.endswith(b"\n")


# ---------------------------------------------------------------------------
# read_multiline_prompt — stdin handling
# ---------------------------------------------------------------------------


def test_read_multiline_prompt_returns_none_on_immediate_eof():
    assert read_multiline_prompt("> ", in_stream=io.StringIO("")) is None


def test_read_multiline_prompt_collects_until_blank_line():
    stream = io.StringIO("hello\nworld\n\nnext line\n")
    text = read_multiline_prompt("> ", in_stream=stream)
    assert text == "hello\nworld"


def test_read_multiline_prompt_skips_leading_blank_line():
    stream = io.StringIO("\nfirst\n\n")
    text = read_multiline_prompt("> ", in_stream=stream)
    assert text == "first"


def test_read_multiline_prompt_single_line_plus_eof():
    stream = io.StringIO("just one line\n")
    text = read_multiline_prompt("> ", in_stream=stream)
    # EOF after one line → returns the collected line
    assert text == "just one line"


# ---------------------------------------------------------------------------
# run_local_loop — end-to-end with fake spawn
# ---------------------------------------------------------------------------


_CANONICAL_STREAM_JSON = b"""\
{"type":"system","subtype":"init","session_id":"runtime-s1","model":"claude-opus","cwd":"/w","claude_code_version":"2.1.101","tools":["Bash"]}
{"type":"assistant","message":{"id":"msg_1","content":[{"type":"text","text":"Hi Pedro"}]}}
{"type":"result","subtype":"success","result":"Hi Pedro","stop_reason":"end_turn","duration_ms":100,"num_turns":1,"total_cost_usd":0.001,"permission_denials":[]}
"""


class _FakeProcess:
    """Stand-in for :class:`asyncio.subprocess.Process`.

    MUST be constructed inside a running asyncio event loop — on
    Python 3.14 :class:`asyncio.StreamReader` no longer lazily binds
    to a loop.
    """

    def __init__(self, stdout_bytes: bytes) -> None:
        loop = asyncio.get_running_loop()
        self.stdout = asyncio.StreamReader(loop=loop)
        self.stdout.feed_data(stdout_bytes)
        self.stdout.feed_eof()

        self._stdin_buffer = bytearray()

        class _StdinWriter:
            def __init__(self, buf: bytearray) -> None:
                self._buf = buf
                self._closed = False

            def write(self, data: bytes) -> None:
                if self._closed:
                    raise BrokenPipeError
                self._buf.extend(data)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self._closed = True

        self.stdin = _StdinWriter(self._stdin_buffer)
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9

    @property
    def stdin_writes(self) -> bytes:
        return bytes(self._stdin_buffer)


def test_run_local_loop_pretty_prints_events_and_exits_on_eof(monkeypatch):
    """End-to-end: spawn a fake claude, pipe a canonical stream-json
    session through, drive the prompt loop with a stdin that
    immediately EOFs, collect the stderr output."""

    captured_fake: dict[str, _FakeProcess] = {}

    async def _fake_spawn(
        config,
        session_id,
        *,
        stdin_stream_json,
        settings_path=None,
    ):
        assert stdin_stream_json is True
        fake = _FakeProcess(_CANONICAL_STREAM_JSON)
        captured_fake["proc"] = fake
        return fake

    import mars.runtime_local as rl_mod

    monkeypatch.setattr(rl_mod, "spawn_claude_code", _fake_spawn)

    out = io.StringIO()
    # stdin EOFs immediately → prompt loop closes claude stdin right away
    in_stream = io.StringIO("")

    async def _go() -> int:
        return await run_local_loop(
            _config(),
            in_stream=in_stream,
            out_stream=out,
        )

    exit_code = asyncio.run(_go())
    rendered = out.getvalue()

    assert exit_code == 0
    assert "session started" in rendered
    assert "claude-opus" in rendered
    assert "Hi Pedro" in rendered  # AssistantText line
    assert "session ended" in rendered
    assert "end_turn" in rendered


# NOTE: a full-loop "writes to stdin" test would race — the fake
# stdout feeds EOF immediately, so the reader task finishes before
# the executor-wrapped prompt loop ever pumps. The encode helper is
# covered directly by `test_encode_user_event_line_*` above; the
# integration of encode → write → drain belongs in the live contract
# test against a real claude CLI rather than the stub harness.


def test_run_local_loop_handles_critical_parse_error_from_malformed_init(
    monkeypatch,
):
    """If claude emits a broken system.init, run_local_loop returns
    non-zero instead of hanging forever."""

    bad_bytes = (
        b'{"type":"system","subtype":"init"}\n'  # missing cwd/model/version
        b'{"type":"result","subtype":"success","result":"x","stop_reason":"end_turn"}\n'
    )

    async def _fake_spawn(*args, **kwargs):
        return _FakeProcess(bad_bytes)

    import mars.runtime_local as rl_mod

    monkeypatch.setattr(rl_mod, "spawn_claude_code", _fake_spawn)

    async def _go() -> int:
        return await run_local_loop(
            _config(),
            in_stream=io.StringIO(""),
            out_stream=io.StringIO(),
        )

    assert asyncio.run(_go()) == 2
