"""Unit tests for ``session.claude_code_stream`` — the stream-json parser.

Story 1.2 happy-path coverage: drive the parser with the spike-2 fixture
(``tests/contract/fixtures/stream_json_sample.jsonl``) and assert that
every canonical event maps to the right Mars event subtype in the right
order. Story 1.3 adds a contract test that runs the real ``claude`` CLI
in CI — this file stays offline.
"""

from __future__ import annotations

import asyncio
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
from session.claude_code_stream import (
    CriticalParseError,
    ParseError,
    parse_line,
    parse_stream,
)

FIXTURE = (
    Path(__file__).parent.parent / "contract" / "fixtures" / "stream_json_sample.jsonl"
)

MARS_SESSION_ID = "mars-sess-1"


# ---------------------------------------------------------------------------
# Fixture-driven happy path
# ---------------------------------------------------------------------------


def _parse_fixture_sync() -> list:
    events = []
    for line in FIXTURE.read_text().splitlines():
        events.extend(parse_line(MARS_SESSION_ID, line))
    return events


def test_fixture_maps_to_canonical_mars_event_sequence():
    events = _parse_fixture_sync()

    # Expected: system.init → assistant(tool_use) → [rate_limit drops]
    # → user(tool_result) → assistant(text) → result.success
    assert [type(ev).__name__ for ev in events] == [
        "SessionStarted",
        "ToolCall",
        "ToolResult",
        "AssistantText",
        "SessionEnded",
    ]


def test_session_started_carries_system_init_fields():
    events = _parse_fixture_sync()
    started = events[0]
    assert isinstance(started, SessionStarted)
    assert started.session_id == MARS_SESSION_ID
    assert started.claude_code_version == "2.1.101"
    assert started.cwd.endswith("/mars-daemons")
    assert "claude-opus" in started.model
    assert "Bash" in started.tools_available


def test_tool_call_fields():
    events = _parse_fixture_sync()
    call = events[1]
    assert isinstance(call, ToolCall)
    assert call.session_id == MARS_SESSION_ID
    assert call.tool_name == "Bash"
    assert call.input["command"] == "echo hello from mars spike"
    assert call.tool_use_id.startswith("toolu_")
    # Correlation fields populated from the runtime message envelope
    assert call.message_id is not None and call.message_id.startswith("msg_")
    assert call.block_index == 0


def test_tool_result_fields_and_pairing():
    events = _parse_fixture_sync()
    call = events[1]
    result = events[2]
    assert isinstance(result, ToolResult)
    assert result.tool_use_id == call.tool_use_id
    assert result.content == "hello from mars spike"
    assert result.is_error is False
    assert result.block_index == 0


def test_assistant_text_after_tool_result():
    events = _parse_fixture_sync()
    text_ev = events[3]
    assert isinstance(text_ev, AssistantText)
    assert "hello from mars spike" in text_ev.text
    assert text_ev.message_id is not None
    assert text_ev.block_index == 0


def test_session_ended_terminal_fields():
    events = _parse_fixture_sync()
    ended = events[-1]
    assert isinstance(ended, SessionEnded)
    assert ended.stop_reason == "end_turn"
    assert ended.num_turns == 2
    assert ended.duration_ms is not None and ended.duration_ms > 0
    assert ended.total_cost_usd is not None and ended.total_cost_usd > 0.0
    assert ended.result is not None and "hello from mars spike" in ended.result
    assert ended.permission_denials == []


def test_every_event_carries_mars_session_id_not_runtime_session_id():
    events = _parse_fixture_sync()
    runtime_sid = json.loads(FIXTURE.read_text().splitlines()[0])["session_id"]
    # The runtime's UUID session id lives inside system.init but must NOT
    # leak onto any Mars event as the primary session_id.
    for ev in events:
        assert ev.session_id == MARS_SESSION_ID
        assert ev.session_id != runtime_sid


# ---------------------------------------------------------------------------
# Event dropping
# ---------------------------------------------------------------------------


def test_rate_limit_event_is_dropped():
    line = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed"},
            "uuid": "u1",
            "session_id": "runtime-1",
        }
    )
    assert parse_line(MARS_SESSION_ID, line) == []


def test_hook_lifecycle_events_are_dropped():
    started = json.dumps(
        {"type": "system", "subtype": "hook_started", "hook_id": "h1"}
    )
    response = json.dumps(
        {"type": "system", "subtype": "hook_response", "hook_id": "h1"}
    )
    assert parse_line(MARS_SESSION_ID, started) == []
    assert parse_line(MARS_SESSION_ID, response) == []


def test_unknown_event_type_is_dropped_silently():
    line = json.dumps({"type": "gremlin", "payload": 42})
    assert parse_line(MARS_SESSION_ID, line) == []


def test_assistant_thinking_only_message_yields_nothing():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [{"type": "thinking", "thinking": "deep thoughts"}],
            },
        }
    )
    assert parse_line(MARS_SESSION_ID, line) == []


def test_assistant_thinking_and_tool_use_mixed_yields_only_tool_call():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {"type": "thinking", "thinking": "plan"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {"command": "echo ok"},
                    },
                ],
            },
        }
    )
    out = parse_line(MARS_SESSION_ID, line)
    assert len(out) == 1
    assert isinstance(out[0], ToolCall)
    # block_index reflects position in the original runtime content array
    assert out[0].block_index == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_blank_and_whitespace_lines_yield_nothing():
    assert parse_line(MARS_SESSION_ID, "") == []
    assert parse_line(MARS_SESSION_ID, "   \n") == []


def test_malformed_json_raises_decode_error():
    with pytest.raises(json.JSONDecodeError):
        parse_line(MARS_SESSION_ID, "{not json")


def test_non_object_json_raises_parse_error():
    with pytest.raises(ParseError):
        parse_line(MARS_SESSION_ID, "[1, 2, 3]")
    with pytest.raises(ParseError):
        parse_line(MARS_SESSION_ID, '"just a string"')


def test_system_init_with_missing_fields_raises_critical():
    """system.init without cwd/model/version is a schema contract break —
    the session cannot proceed without SessionStarted anchoring it."""
    line = json.dumps({"type": "system", "subtype": "init"})  # no cwd/model/version
    with pytest.raises(CriticalParseError):
        parse_line(MARS_SESSION_ID, line)


def test_system_init_raises_when_field_is_wrong_type():
    line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "cwd": 42,  # not a string
            "model": "claude-opus-4-6",
            "claude_code_version": "2.1.101",
        }
    )
    with pytest.raises(CriticalParseError):
        parse_line(MARS_SESSION_ID, line)


def test_critical_parse_error_is_not_a_parse_error_subclass():
    """Callers using `except ParseError` must not accidentally swallow
    CriticalParseError — the two exception hierarchies are disjoint."""
    assert not issubclass(CriticalParseError, ParseError)


def test_tool_result_without_tool_use_id_is_dropped():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "x", "is_error": False}]
            },
        }
    )
    assert parse_line(MARS_SESSION_ID, line) == []


def test_tool_result_multipart_content_joins_text_parts():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [
                            {"type": "text", "text": "hello "},
                            {"type": "text", "text": "world"},
                        ],
                        "is_error": False,
                    }
                ]
            },
        }
    )
    out = parse_line(MARS_SESSION_ID, line)
    assert len(out) == 1
    assert isinstance(out[0], ToolResult)
    assert out[0].content == "hello world"


# ---------------------------------------------------------------------------
# Async parse_stream wrapper
# ---------------------------------------------------------------------------


def test_parse_stream_async_drives_fixture_to_canonical_sequence():
    async def _run() -> list:
        reader = asyncio.StreamReader()
        for line in FIXTURE.read_bytes().splitlines(keepends=True):
            reader.feed_data(line)
        reader.feed_eof()
        out: list = []
        async for ev in parse_stream(MARS_SESSION_ID, reader):
            out.append(ev)
        return out

    events = asyncio.run(_run())
    assert [type(ev).__name__ for ev in events] == [
        "SessionStarted",
        "ToolCall",
        "ToolResult",
        "AssistantText",
        "SessionEnded",
    ]


def test_parse_stream_swallows_malformed_lines():
    async def _run() -> list:
        reader = asyncio.StreamReader()
        reader.feed_data(b"{not json\n")
        reader.feed_data(b"[1, 2]\n")
        reader.feed_data(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "cwd": "/workspace",
                    "model": "claude-opus-4-6",
                    "claude_code_version": "2.1.101",
                    "tools": ["Bash"],
                }
            ).encode()
            + b"\n"
        )
        reader.feed_eof()
        out: list = []
        async for ev in parse_stream(MARS_SESSION_ID, reader):
            out.append(ev)
        return out

    events = asyncio.run(_run())
    assert len(events) == 1
    assert isinstance(events[0], SessionStarted)


def test_parse_stream_propagates_critical_parse_error():
    """CriticalParseError is NOT swallowed by parse_stream — it must
    reach the supervisor so it can kill the broken session."""

    async def _run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            json.dumps({"type": "system", "subtype": "init"}).encode() + b"\n"
        )
        reader.feed_eof()
        async for _ in parse_stream(MARS_SESSION_ID, reader):
            pass

    with pytest.raises(CriticalParseError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Strict boolean + strict dict coercion (from codex review)
# ---------------------------------------------------------------------------


def test_tool_result_is_error_only_accepts_literal_true():
    """`is_error` must be the literal JSON boolean `true`, not a
    truthy string or integer. Prevents `"false"` being parsed as an error.
    """
    # Literal true → is_error True
    line_true = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "x",
                        "is_error": True,
                    }
                ]
            },
        }
    )
    [ev] = parse_line(MARS_SESSION_ID, line_true)
    assert isinstance(ev, ToolResult)
    assert ev.is_error is True

    # String "false" must NOT be coerced to True
    line_false_str = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "x",
                        "is_error": "false",
                    }
                ]
            },
        }
    )
    [ev2] = parse_line(MARS_SESSION_ID, line_false_str)
    assert ev2.is_error is False

    # Integer 1 must NOT be coerced to True either
    line_int = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "x",
                        "is_error": 1,
                    }
                ]
            },
        }
    )
    [ev3] = parse_line(MARS_SESSION_ID, line_int)
    assert ev3.is_error is False


def test_non_dict_tool_input_logs_warning_and_falls_back_to_empty(caplog):
    """If runtime emits a tool_use.input that is not a dict, v1.2 still
    emits the ToolCall but logs a warning so the drift is visible."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": "echo hi",  # string, not a dict
                    }
                ],
            },
        }
    )
    with caplog.at_level("WARNING", logger="session.claude_code_stream"):
        out = parse_line(MARS_SESSION_ID, line)
    assert len(out) == 1
    assert isinstance(out[0], ToolCall)
    assert out[0].input == {}
    assert any(
        "tool_use.input is not a dict" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# stream_event — --include-partial-messages AssistantChunk events
# ---------------------------------------------------------------------------


def test_stream_event_text_delta_yields_assistant_chunk():
    """Anthropic SSE content_block_delta.text_delta → AssistantChunk."""
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            "session_id": "runtime-1",
        }
    )
    out = parse_line(MARS_SESSION_ID, line)
    assert len(out) == 1
    chunk = out[0]
    assert isinstance(chunk, AssistantChunk)
    assert chunk.delta == "Hello"
    assert chunk.block_index == 0
    # Stateless parser — chunks don't carry a message_id
    assert chunk.message_id is None
    assert chunk.ephemeral is True
    assert chunk.durable is False


def test_stream_event_empty_text_delta_is_dropped():
    """Empty text_delta chunks waste bandwidth — drop them."""
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": ""},
            },
        }
    )
    assert parse_line(MARS_SESSION_ID, line) == []


def test_stream_event_non_text_delta_types_are_dropped():
    """message_start, content_block_start/stop, message_delta/stop,
    input_json_delta: not yet mapped to Mars events, dropped silently."""
    for inner_type in (
        "message_start",
        "content_block_start",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ):
        line = json.dumps(
            {
                "type": "stream_event",
                "event": {"type": inner_type, "index": 0},
            }
        )
        assert parse_line(MARS_SESSION_ID, line) == [], f"expected drop for {inner_type}"


def test_stream_event_input_json_delta_is_dropped():
    """Tool input streaming isn't wired to Mars chunks in v1."""
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "{\"com"},
            },
        }
    )
    assert parse_line(MARS_SESSION_ID, line) == []


def test_stream_event_with_bad_block_index_falls_back_to_none():
    """strict=True on AssistantChunk.block_index rejects bools and
    negatives; the parser should defensively coerce to None instead
    of raising."""
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": -1,  # negative → not usable
                "delta": {"type": "text_delta", "text": "hi"},
            },
        }
    )
    [ev] = parse_line(MARS_SESSION_ID, line)
    assert isinstance(ev, AssistantChunk)
    assert ev.block_index is None


# ---------------------------------------------------------------------------
# on_warning callback (parse_stream)
# ---------------------------------------------------------------------------


def test_parse_stream_invokes_on_warning_for_malformed_lines():
    """Soft drops surface via the callback instead of stdlib logging so
    the supervisor has a structured hook."""

    seen: list[tuple[str, BaseException | None]] = []

    async def _run() -> list:
        reader = asyncio.StreamReader()
        reader.feed_data(b"{not json\n")
        reader.feed_data(b"[1, 2]\n")
        reader.feed_data(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "cwd": "/workspace",
                    "model": "claude-opus-4-6",
                    "claude_code_version": "2.1.101",
                    "tools": ["Bash"],
                }
            ).encode()
            + b"\n"
        )
        reader.feed_eof()
        out: list = []
        async for ev in parse_stream(
            MARS_SESSION_ID,
            reader,
            on_warning=lambda m, e: seen.append((m, e)),
        ):
            out.append(ev)
        return out

    events = asyncio.run(_run())
    assert len(events) == 1
    assert isinstance(events[0], SessionStarted)
    # Two warnings: one for json decode, one for non-object ParseError
    assert len(seen) == 2
    reasons = [msg for msg, _ in seen]
    assert any("json decode error" in r for r in reasons)
    assert any("parse error" in r for r in reasons)
    exceptions = [exc for _, exc in seen]
    assert any(isinstance(e, json.JSONDecodeError) for e in exceptions)
    assert any(isinstance(e, ParseError) for e in exceptions)


def test_tool_result_image_only_content_yields_placeholder(caplog):
    """All-non-text tool_result content collapses to a sentinel so the
    drop is visible in the chat instead of becoming an empty string."""
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [
                            {"type": "image", "source": {"data": "abc"}},
                        ],
                        "is_error": False,
                    }
                ]
            },
        }
    )
    with caplog.at_level("WARNING", logger="session.claude_code_stream"):
        [ev] = parse_line(MARS_SESSION_ID, line)
    assert isinstance(ev, ToolResult)
    assert "non-text" in ev.content
    assert any("non-text" in rec.message for rec in caplog.records)
