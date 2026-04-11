"""Unit tests for ``events.types`` — the Mars event hierarchy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from events.types import (
    DURABLE_EVENT_TYPES,
    EPHEMERAL_EVENT_TYPES,
    EVENT_ASSISTANT_CHUNK,
    EVENT_ASSISTANT_TEXT,
    EVENT_PERMISSION_REQUEST,
    EVENT_SESSION_ENDED,
    EVENT_SESSION_STARTED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_STARTED,
    EVENT_TURN_COMPLETED,
    MARS_EVENT_ADAPTER,
    AssistantChunk,
    AssistantText,
    MarsEventBase,
    PermissionRequest,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
    ToolStarted,
    TurnCompleted,
    is_durable,
    is_ephemeral,
)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_durable_and_ephemeral_sets_are_disjoint():
    assert DURABLE_EVENT_TYPES.isdisjoint(EPHEMERAL_EVENT_TYPES)


def test_every_concrete_subtype_is_classified_exactly_once():
    every_type = {
        EVENT_SESSION_STARTED,
        EVENT_SESSION_ENDED,
        EVENT_ASSISTANT_TEXT,
        EVENT_ASSISTANT_CHUNK,
        EVENT_TOOL_STARTED,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        EVENT_PERMISSION_REQUEST,
        EVENT_TURN_COMPLETED,
    }
    assert every_type == DURABLE_EVENT_TYPES | EPHEMERAL_EVENT_TYPES


def test_state_transition_events_are_durable():
    # State-transition events are durable: they must be persisted before
    # emit so browsers can replay them on reconnect.
    assert is_durable(EVENT_SESSION_STARTED)
    assert is_durable(EVENT_SESSION_ENDED)
    assert is_durable(EVENT_ASSISTANT_TEXT)
    assert is_durable(EVENT_TOOL_CALL)
    assert is_durable(EVENT_TOOL_RESULT)
    assert is_durable(EVENT_PERMISSION_REQUEST)
    assert is_durable(EVENT_TURN_COMPLETED)
    for t in (EVENT_SESSION_STARTED, EVENT_TOOL_CALL):
        assert not is_ephemeral(t)


def test_ephemeral_set_is_chunks_and_started_markers():
    assert is_ephemeral(EVENT_ASSISTANT_CHUNK)
    assert is_ephemeral(EVENT_TOOL_STARTED)
    for t in (EVENT_ASSISTANT_CHUNK, EVENT_TOOL_STARTED):
        assert not is_durable(t)


def test_classifier_on_unknown_event_type_returns_false_for_both():
    # Unknown events must never be accidentally classified as durable; the
    # forwarder should treat them as unknown and refuse to persist.
    assert not is_durable("surprise")
    assert not is_ephemeral("surprise")


# ---------------------------------------------------------------------------
# Construction + durable/ephemeral properties
# ---------------------------------------------------------------------------


def test_session_started_construction_and_durable_flag():
    ev = SessionStarted(
        session_id="s-1",
        model="claude-opus-4-6",
        cwd="/workspace",
        claude_code_version="2.1.101",
        tools_available=["Bash", "Edit"],
    )
    assert ev.type == EVENT_SESSION_STARTED
    assert ev.durable is True
    assert ev.ephemeral is False
    assert ev.sequence is None  # assigned at publish time


def test_assistant_chunk_is_ephemeral():
    ev = AssistantChunk(session_id="s-1", delta="hel")
    assert ev.type == EVENT_ASSISTANT_CHUNK
    assert ev.durable is False
    assert ev.ephemeral is True


def test_tool_started_is_ephemeral():
    ev = ToolStarted(session_id="s-1", tool_use_id="tu-1", tool_name="Bash")
    assert ev.durable is False
    assert ev.ephemeral is True


def test_timestamp_is_utc_aware_datetime():
    ev = AssistantText(session_id="s-1", text="hi")
    assert isinstance(ev.timestamp, datetime)
    assert ev.timestamp.tzinfo is not None
    assert ev.timestamp.utcoffset() == timezone.utc.utcoffset(None)


def test_timestamp_json_round_trip_preserves_utc():
    ev = AssistantText(session_id="s-1", text="hi")
    dumped = MARS_EVENT_ADAPTER.dump_python(ev, mode="json")
    assert isinstance(dumped["timestamp"], str)
    reparsed = MARS_EVENT_ADAPTER.validate_python(dumped)
    assert isinstance(reparsed.timestamp, datetime)
    assert reparsed.timestamp.tzinfo is not None
    assert reparsed.timestamp == ev.timestamp


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        AssistantText(session_id="s-1", text="hi", extra="nope")  # type: ignore[call-arg]


def test_base_model_is_not_directly_constructible_without_required_fields():
    with pytest.raises(ValidationError):
        MarsEventBase()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Discriminator-based parsing (the fixture round-trip)
# ---------------------------------------------------------------------------


def _sample_events() -> list[dict]:
    """Minimal payloads covering every subtype — the fixture round-trip."""
    return [
        {
            "session_id": "s-1",
            "type": "session_started",
            "model": "claude-opus-4-6",
            "cwd": "/workspace",
            "claude_code_version": "2.1.101",
            "tools_available": ["Bash"],
        },
        {"session_id": "s-1", "type": "assistant_text", "text": "hi"},
        {"session_id": "s-1", "type": "assistant_chunk", "delta": "hi"},
        {
            "session_id": "s-1",
            "type": "tool_started",
            "tool_use_id": "tu-1",
            "tool_name": "Bash",
        },
        {
            "session_id": "s-1",
            "type": "tool_call",
            "tool_use_id": "tu-1",
            "tool_name": "Bash",
            "input": {"command": "echo hi"},
        },
        {
            "session_id": "s-1",
            "type": "tool_result",
            "tool_use_id": "tu-1",
            "content": "hi\n",
            "is_error": False,
        },
        {
            "session_id": "s-1",
            "type": "permission_request",
            "tool_use_id": "tu-2",
            "tool_name": "Edit",
            "input": {"file_path": "CLAUDE.md"},
            "reason": "denied by PreToolUse hook",
        },
        {
            "session_id": "s-1",
            "type": "turn_completed",
            "stop_reason": "end_turn",
            "num_turns": 1,
        },
        {
            "session_id": "s-1",
            "type": "session_ended",
            "result": "hi",
            "stop_reason": "end_turn",
            "duration_ms": 1234,
            "num_turns": 1,
            "total_cost_usd": 0.001,
            "permission_denials": [],
        },
    ]


_EXPECTED_CLASSES = {
    "session_started": SessionStarted,
    "assistant_text": AssistantText,
    "assistant_chunk": AssistantChunk,
    "tool_started": ToolStarted,
    "tool_call": ToolCall,
    "tool_result": ToolResult,
    "permission_request": PermissionRequest,
    "turn_completed": TurnCompleted,
    "session_ended": SessionEnded,
}


def test_discriminator_routes_every_subtype():
    for payload in _sample_events():
        parsed = MARS_EVENT_ADAPTER.validate_python(payload)
        expected_cls = _EXPECTED_CLASSES[payload["type"]]
        assert isinstance(parsed, expected_cls)
        assert parsed.type == payload["type"]


def test_fixture_event_round_trips_through_json():
    """Every subtype can be dumped to JSON-safe dict and re-parsed losslessly."""
    for payload in _sample_events():
        parsed = MARS_EVENT_ADAPTER.validate_python(payload)
        dumped = MARS_EVENT_ADAPTER.dump_python(parsed, mode="json")
        reparsed = MARS_EVENT_ADAPTER.validate_python(dumped)
        assert type(reparsed) is type(parsed)
        # Semantic equality on user-supplied fields (drop timestamp which is
        # auto-generated and will differ across construction sites)
        parsed_no_ts = parsed.model_dump(exclude={"timestamp"})
        reparsed_no_ts = reparsed.model_dump(exclude={"timestamp"})
        assert parsed_no_ts == reparsed_no_ts


def test_unknown_type_is_rejected_by_the_discriminator():
    with pytest.raises(ValidationError):
        MARS_EVENT_ADAPTER.validate_python(
            {"session_id": "s-1", "type": "gremlin", "delta": "x"}
        )


# ---------------------------------------------------------------------------
# Strictness / boundary-schema invariants (from codex adversarial review)
# ---------------------------------------------------------------------------


def test_ephemeral_event_with_sequence_is_rejected():
    """Sequence numbers only belong on durable events."""
    with pytest.raises(ValidationError):
        AssistantChunk(session_id="s-1", delta="hi", sequence=1)
    with pytest.raises(ValidationError):
        ToolStarted(
            session_id="s-1", tool_use_id="tu-1", tool_name="Bash", sequence=1
        )


def test_durable_event_may_have_null_sequence_at_construction():
    """Pending-publish state is legal; supervisor assigns sequence later."""
    ev = AssistantText(session_id="s-1", text="hi")
    assert ev.sequence is None
    ev_with_seq = AssistantText(session_id="s-1", text="hi", sequence=42)
    assert ev_with_seq.sequence == 42


def test_strict_numeric_rejects_bool_and_string_coercion():
    """Boundary schema must reject sloppy numeric coercion."""
    with pytest.raises(ValidationError):
        AssistantText(session_id="s-1", text="hi", sequence=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AssistantText(session_id="s-1", text="hi", sequence="1")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SessionEnded(session_id="s-1", duration_ms=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SessionEnded(session_id="s-1", total_cost_usd="0.1")  # type: ignore[arg-type]


def test_strict_numeric_rejects_negative_values():
    with pytest.raises(ValidationError):
        AssistantText(session_id="s-1", text="hi", sequence=-1)
    with pytest.raises(ValidationError):
        SessionEnded(session_id="s-1", duration_ms=-1)


def test_json_value_rejects_non_json_safe_tool_input():
    """dict[str, JsonValue] must reject non-JSON-safe payloads."""

    class NotJson:
        pass

    with pytest.raises(ValidationError):
        ToolCall(
            session_id="s-1",
            tool_use_id="tu-1",
            tool_name="Bash",
            input={"command": NotJson()},  # type: ignore[dict-item]
        )


def test_correlation_fields_round_trip_on_per_block_events():
    """message_id + block_index let the UI reconstruct multi-block turns."""
    call = ToolCall(
        session_id="s-1",
        tool_use_id="tu-1",
        tool_name="Bash",
        input={"command": "echo hi"},
        message_id="msg_01abc",
        block_index=2,
    )
    dumped = MARS_EVENT_ADAPTER.dump_python(call, mode="json")
    assert dumped["message_id"] == "msg_01abc"
    assert dumped["block_index"] == 2
    reparsed = MARS_EVENT_ADAPTER.validate_python(dumped)
    assert isinstance(reparsed, ToolCall)
    assert reparsed.message_id == "msg_01abc"
    assert reparsed.block_index == 2
