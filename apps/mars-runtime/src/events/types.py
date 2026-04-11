"""Mars event type hierarchy for the runtime supervisor.

Every event produced by a Mars daemon session flows through this module.
The shape mirrors Camtom's pattern at
``services/fastapi/src/products/agents/agent/events.py`` (lines 18-111):
a set of string-constant event types, a split between *durable* and
*ephemeral* classes, and classifier helpers. We deliberately copy that
shape so the same mental model carries over for contributors who already
worked in Camtom.

Differences from Camtom:

* Mars events are typed Pydantic models (not plain dicts). Each concrete
  subtype is a subclass of :class:`MarsEventBase` with its own payload
  fields, and the union is exposed via the discriminated
  :data:`MarsEvent` type alias so parsers / HTTP forwarders can validate
  arbitrary JSON payloads into the right subclass.
* Mars is session-oriented, not conversation-oriented. Every event carries
  ``session_id``.
* ``sequence`` is optional on construction and assigned at publish time by
  the supervisor for durable events. Ephemeral events never receive one.

The durable / ephemeral split is the contract with the HTTP event
forwarder (Epic 2) and the control plane's SSE fanout: durable events
MUST be persisted before emit so browsers can replay them on reconnect;
ephemeral events are safe to drop on backpressure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

# ---------------------------------------------------------------------------
# Event type string constants
#
# These strings are the stable wire contract. Do not rename them without a
# coordinated rollout across supervisor + control plane + any stored events.
# ---------------------------------------------------------------------------

EVENT_SESSION_STARTED = "session_started"
EVENT_SESSION_ENDED = "session_ended"
EVENT_ASSISTANT_TEXT = "assistant_text"
EVENT_ASSISTANT_CHUNK = "assistant_chunk"
EVENT_TOOL_STARTED = "tool_started"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_PERMISSION_REQUEST = "permission_request"
EVENT_TURN_COMPLETED = "turn_completed"


# Durable events represent state transitions and are persisted before emit.
# Browsers replay them on reconnect from the control plane's event log.
DURABLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_SESSION_STARTED,
        EVENT_SESSION_ENDED,
        EVENT_ASSISTANT_TEXT,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        EVENT_PERMISSION_REQUEST,
        EVENT_TURN_COMPLETED,
    }
)

# Ephemeral events are fire-and-forget streaming markers. They are emitted
# directly by the sink, never persisted, and safe to drop on backpressure.
EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_ASSISTANT_CHUNK,
        EVENT_TOOL_STARTED,
    }
)


def is_durable(event_type: str) -> bool:
    """Return True if ``event_type`` must be persisted before emit."""
    return event_type in DURABLE_EVENT_TYPES


def is_ephemeral(event_type: str) -> bool:
    """Return True if ``event_type`` is fire-and-forget (safe to drop)."""
    return event_type in EPHEMERAL_EVENT_TYPES


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """UTC-aware datetime, the canonical timestamp for every event."""
    return datetime.now(timezone.utc)


class MarsEventBase(BaseModel):
    """Shared shape for every Mars event.

    Attributes:
        session_id: Opaque identifier of the Mars daemon session that
            produced the event. Distinct from the Claude Code internal
            ``session_id`` — Mars maintains its own.
        type: Discriminator string. Each subclass pins this to a constant.
        sequence: Monotonic per-session sequence number. ``None`` on
            construction; the supervisor assigns it at publish time for
            durable events (ephemeral events never receive one — enforced
            by :meth:`_check_sequence_durability`).
        timestamp: UTC-aware :class:`datetime`, assigned at construction.
            Serialized as ISO-8601 in JSON mode.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., description="Mars session identifier.")
    type: str = Field(..., description="Event discriminator.")
    sequence: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        description="Assigned at publish time for durable events; None otherwise.",
    )
    timestamp: datetime = Field(
        default_factory=_utcnow,
        description="UTC-aware datetime at construction (ISO-8601 in JSON).",
    )

    @property
    def durable(self) -> bool:
        """Whether this event must be persisted before emit."""
        return is_durable(self.type)

    @property
    def ephemeral(self) -> bool:
        """Whether this event is fire-and-forget."""
        return is_ephemeral(self.type)

    @model_validator(mode="after")
    def _check_sequence_durability(self) -> "MarsEventBase":
        """Ephemeral events must never carry a sequence number.

        Durable events are allowed to have ``sequence is None`` at
        construction (the supervisor assigns it at publish time); we only
        reject the invariant violation, not the pending state.
        """
        if self.sequence is not None and is_ephemeral(self.type):
            raise ValueError(
                f"event type {self.type!r} is ephemeral and must not carry a "
                f"sequence number; got sequence={self.sequence}"
            )
        return self


# ---------------------------------------------------------------------------
# Concrete event subtypes
# ---------------------------------------------------------------------------


class SessionStarted(MarsEventBase):
    """Emitted once when the supervisor spawns a ``claude`` subprocess and
    receives the first ``system.init`` stream-json event.
    """

    type: Literal["session_started"] = Field(default=EVENT_SESSION_STARTED)
    model: str = Field(..., description="LLM model name reported by the runtime.")
    cwd: str = Field(..., description="Working directory inside the machine.")
    claude_code_version: str = Field(
        ..., description="Pinned Claude Code CLI version that produced the stream."
    )
    tools_available: list[str] = Field(
        default_factory=list,
        description="Tool names the runtime exposed to the model at session start.",
    )


class SessionEnded(MarsEventBase):
    """Emitted when the ``claude`` subprocess finishes, crashes, or is
    killed by the supervisor. Always terminal — no events follow this one
    for the session.
    """

    type: Literal["session_ended"] = Field(default=EVENT_SESSION_ENDED)
    result: str | None = Field(
        default=None,
        description="Final text payload from the runtime's ``result`` event, if any.",
    )
    stop_reason: str | None = Field(
        default=None, description="end_turn | max_tokens | killed | error | ..."
    )
    duration_ms: int | None = Field(default=None, strict=True, ge=0)
    num_turns: int | None = Field(default=None, strict=True, ge=0)
    total_cost_usd: float | None = Field(default=None, strict=True, ge=0.0)
    permission_denials: list[dict[str, JsonValue]] = Field(
        default_factory=list,
        description="Tools the runtime refused to execute during the session.",
    )


class AssistantText(MarsEventBase):
    """A complete assistant text block. Emitted once per text content block
    in an assistant message. Durable — browsers replay these on reconnect.

    ``message_id`` + ``block_index`` let downstream consumers reconstruct
    the original assistant message layout deterministically (the runtime
    can emit multiple ``text`` / ``tool_use`` blocks inside a single
    assistant message).
    """

    type: Literal["assistant_text"] = Field(default=EVENT_ASSISTANT_TEXT)
    text: str = Field(..., description="Finalized text payload.")
    message_id: str | None = Field(
        default=None,
        description="Runtime message id that produced this block (e.g. msg_...).",
    )
    block_index: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        description="0-based index of this block inside the runtime message's content array.",
    )


class AssistantChunk(MarsEventBase):
    """A streaming delta of assistant text. Emitted only when the supervisor
    launches Claude Code with ``--include-partial-messages``. Ephemeral —
    browsers rely on ``AssistantText`` for state, chunks are a UX nicety.
    """

    type: Literal["assistant_chunk"] = Field(default=EVENT_ASSISTANT_CHUNK)
    delta: str = Field(..., description="Text delta to append to the current turn.")
    message_id: str | None = Field(
        default=None,
        description="Runtime message id the delta belongs to.",
    )
    block_index: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        description="0-based index of the block this delta belongs to.",
    )


class ToolStarted(MarsEventBase):
    """A marker emitted when the supervisor observes the runtime about to
    invoke a tool. Ephemeral — ``ToolCall`` carries the authoritative
    payload.
    """

    type: Literal["tool_started"] = Field(default=EVENT_TOOL_STARTED)
    tool_use_id: str = Field(..., description="Runtime-assigned tool call identifier.")
    tool_name: str = Field(..., description="Tool name the model selected.")


class ToolCall(MarsEventBase):
    """A finalized tool invocation. Corresponds to a ``tool_use`` block
    inside an assistant message in the runtime's stream-json output.
    """

    type: Literal["tool_call"] = Field(default=EVENT_TOOL_CALL)
    tool_use_id: str = Field(..., description="Runtime-assigned tool call identifier.")
    tool_name: str = Field(..., description="Tool name the model selected.")
    input: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Tool input payload exactly as emitted by the runtime.",
    )
    message_id: str | None = Field(
        default=None,
        description="Runtime assistant message id that contained this tool_use block.",
    )
    block_index: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        description="0-based index of this tool_use block inside the assistant message.",
    )


class ToolResult(MarsEventBase):
    """A finalized tool result, paired with a prior :class:`ToolCall` by
    ``tool_use_id``. Corresponds to a ``tool_result`` block inside a user
    message in the runtime's stream-json output.
    """

    type: Literal["tool_result"] = Field(default=EVENT_TOOL_RESULT)
    tool_use_id: str = Field(..., description="Matches the ToolCall that produced it.")
    content: str = Field(..., description="Stringified tool output.")
    is_error: bool = Field(
        default=False, description="Whether the runtime flagged the result as an error."
    )
    message_id: str | None = Field(
        default=None,
        description="Runtime user message id that contained this tool_result block.",
    )
    block_index: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        description="0-based index of this tool_result block inside the user message.",
    )


class PermissionRequest(MarsEventBase):
    """A pending tool-approval request surfaced by the supervisor.

    v1 ships with a static-allowlist + PreToolUse-hook model (see
    ``spikes/03-permission-roundtrip.md``), so in practice this event is
    emitted whenever a tool is blocked by the PreToolUse denylist — the
    decision is already final, but the request is recorded so the UI can
    show the user what happened.

    v1.1 will repurpose this event as a true round-trip: the supervisor
    will pause the runtime, emit ``PermissionRequest`` to the control
    plane, wait for an approve/deny response on the
    ``POST /sessions/{id}/permission-response`` endpoint, then inject the
    answer into the runtime via the stream-json input channel.
    """

    type: Literal["permission_request"] = Field(default=EVENT_PERMISSION_REQUEST)
    tool_use_id: str = Field(..., description="Runtime-assigned tool call identifier.")
    tool_name: str = Field(..., description="Tool name the model tried to use.")
    input: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Tool input payload the model wanted to execute.",
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Why the supervisor is surfacing this request (e.g. 'denied by "
            "PreToolUse hook: CLAUDE.md edit')."
        ),
    )


class TurnCompleted(MarsEventBase):
    """A turn boundary marker. Emitted after every assistant message that
    ends a turn (``stop_reason`` non-null in stream-json).
    """

    type: Literal["turn_completed"] = Field(default=EVENT_TURN_COMPLETED)
    stop_reason: str | None = Field(default=None)
    num_turns: int | None = Field(default=None, strict=True, ge=0)


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

#: Discriminated union over every concrete Mars event subtype. Callers that
#: need to parse arbitrary JSON into the right subclass should use
#: :data:`MARS_EVENT_ADAPTER` (a cached :class:`pydantic.TypeAdapter`).
MarsEvent = Annotated[
    Union[
        SessionStarted,
        SessionEnded,
        AssistantText,
        AssistantChunk,
        ToolStarted,
        ToolCall,
        ToolResult,
        PermissionRequest,
        TurnCompleted,
    ],
    Field(discriminator="type"),
]

#: Reusable Pydantic v2 adapter for validating and dumping :data:`MarsEvent`
#: payloads. Shared so the HTTP forwarder and SSE fanout don't construct a
#: new adapter per call.
MARS_EVENT_ADAPTER: TypeAdapter[MarsEvent] = TypeAdapter(MarsEvent)


__all__ = [
    "DURABLE_EVENT_TYPES",
    "EPHEMERAL_EVENT_TYPES",
    "EVENT_ASSISTANT_CHUNK",
    "EVENT_ASSISTANT_TEXT",
    "EVENT_PERMISSION_REQUEST",
    "EVENT_SESSION_ENDED",
    "EVENT_SESSION_STARTED",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_TOOL_STARTED",
    "EVENT_TURN_COMPLETED",
    "MARS_EVENT_ADAPTER",
    "AssistantChunk",
    "AssistantText",
    "MarsEvent",
    "MarsEventBase",
    "PermissionRequest",
    "SessionEnded",
    "SessionStarted",
    "ToolCall",
    "ToolResult",
    "ToolStarted",
    "TurnCompleted",
    "is_durable",
    "is_ephemeral",
]
