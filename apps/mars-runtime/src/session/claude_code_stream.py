"""Parser: Claude Code ``stream-json`` → Mars events.

The supervisor spawns ``claude -p ... --output-format stream-json`` as a
subprocess and feeds its stdout through :func:`parse_stream` to produce an
async iterator of typed :class:`~events.types.MarsEventBase` payloads.

This module is the contract between the pinned, externally-owned Claude
Code CLI and the rest of Mars. It is the highest-risk file in the
runtime; Story 1.3 adds a contract test that runs the real CLI against
the captured spike-2 fixture and fails CI on any schema drift.

Scope — Story 1.2 (happy path)
------------------------------

Handles the canonical event sequence observed in the spike-2 fixture
(``tests/contract/fixtures/stream_json_sample.jsonl``):

    system.init → assistant(tool_use) → rate_limit_event
    → user(tool_result) → assistant(text) → result.success

and produces, in order: :class:`~events.types.SessionStarted`,
:class:`~events.types.ToolCall`, (nothing for the rate_limit_event),
:class:`~events.types.ToolResult`, :class:`~events.types.AssistantText`,
:class:`~events.types.SessionEnded`.

Known runtime events that Mars intentionally drops in v1.2:

* ``rate_limit_event`` — quota / overage status, Mars exposes cost /
  usage via :class:`SessionEnded` instead.
* ``system.hook_started`` / ``system.hook_response`` — user-global hook
  lifecycle; not present in clean Mars containers but tolerated here
  defensively.
* assistant ``thinking`` blocks — internal model reasoning; not
  user-visible and not persisted in v1.

Out of scope (Story 1.3)
------------------------

* ``--include-partial-messages`` assistant chunks
* Multi-part tool_result content (list of text/image blocks — v1.2
  collapses to a joined string best-effort but does not emit structured
  blocks)
* Error handling with a warning callback — v1.2 silently drops lines
  that fail JSON decode, have an unexpected shape, or reference an
  unknown event type
* Contract test that runs the real CLI in CI
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import AsyncIterator, Callable

from events.types import (
    AssistantText,
    MarsEventBase,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
)

__all__ = [
    "CriticalParseError",
    "ParseError",
    "parse_line",
    "parse_stream",
]

_log = logging.getLogger(__name__)


class ParseError(ValueError):
    """Recoverable parser error — an individual line could not be decoded
    as a JSON object. :func:`parse_stream` swallows these so one bad line
    does not kill the whole session stream.
    """


class CriticalParseError(Exception):
    """Unrecoverable parser error — the runtime emitted a canonical event
    (``system.init``, ``result.*``) with a malformed or missing payload.

    This is a contract break with the pinned Claude Code CLI — the
    session cannot proceed without a :class:`SessionStarted` anchoring
    every subsequent event. :func:`parse_stream` deliberately does NOT
    catch this exception: it propagates up to the supervisor, which
    should kill the session and surface the error to the control plane.

    Does NOT inherit from :class:`ParseError` so ``except ParseError``
    handlers do not accidentally swallow it.
    """


# A handler takes (mars_session_id, decoded_payload) and returns a list of
# MarsEvent subclasses. Returning an empty list means "drop this event".
Handler = Callable[[str, dict], Iterable[MarsEventBase]]


def parse_line(session_id: str, line: str) -> list[MarsEventBase]:
    """Translate one stream-json line into zero or more Mars events.

    Args:
        session_id: The Mars-assigned session identifier that will be
            stamped onto every emitted event. Distinct from the runtime's
            internal ``session_id`` (which lives inside ``system.init``
            and is carried on :class:`SessionStarted` as context only).
        line: One JSON-encoded line from the runtime's ``stream-json``
            output. Trailing whitespace and blank lines are tolerated.

    Returns:
        A list of Mars events in the order they should be emitted. May
        be empty for lines that represent events Mars intentionally drops
        (``rate_limit_event``, hook lifecycle, thinking-only assistant
        turns, unknown event types).

    Raises:
        json.JSONDecodeError: if ``line`` contains malformed JSON.
        ParseError: if ``line`` decodes to a non-object (list, scalar).
    """
    stripped = line.strip()
    if not stripped:
        return []
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ParseError(
            f"expected stream-json event to be a JSON object, got {type(payload).__name__}"
        )

    ev_type = payload.get("type")
    subtype = payload.get("subtype")

    handler = _DISPATCH.get((ev_type, subtype)) or _DISPATCH.get((ev_type, None))
    if handler is None:
        return []
    return list(handler(session_id, payload))


async def parse_stream(
    session_id: str,
    stdout: asyncio.StreamReader,
) -> AsyncIterator[MarsEventBase]:
    """Consume a JSONL byte stream and yield typed Mars events.

    The supervisor wires this to the stdout of a
    ``claude -p ... --output-format stream-json`` subprocess. Each line
    is decoded as UTF-8, stripped, and passed to :func:`parse_line`.
    Lines that fail to decode or that :func:`parse_line` rejects with
    :class:`json.JSONDecodeError` / :class:`ParseError` are silently
    dropped in v1.2; Story 1.3 adds a warning callback.

    Args:
        session_id: Mars-assigned session identifier, stamped on every
            emitted event.
        stdout: An :class:`asyncio.StreamReader` connected to the
            runtime subprocess's stdout. The stream must signal EOF
            (via ``feed_eof`` in tests or process exit in production)
            for the iterator to terminate.

    Yields:
        Typed Mars events in the order they arrive on the stream.
    """
    while True:
        line_bytes = await stdout.readline()
        if not line_bytes:
            return
        try:
            decoded = line_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            events = parse_line(session_id, decoded)
        except (json.JSONDecodeError, ParseError):
            # v1.2: swallow malformed lines. v1.3 adds a warning hook.
            continue
        for ev in events:
            yield ev


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_system_init(session_id: str, payload: dict) -> list[MarsEventBase]:
    tools_raw = payload.get("tools") or []
    tools: list[str] = [str(t) for t in tools_raw if isinstance(t, (str, bytes))]
    model = payload.get("model")
    cwd = payload.get("cwd")
    version = payload.get("claude_code_version")

    missing: list[str] = []
    if not isinstance(model, str):
        missing.append("model")
    if not isinstance(cwd, str):
        missing.append("cwd")
    if not isinstance(version, str):
        missing.append("claude_code_version")
    if missing:
        raise CriticalParseError(
            "system.init payload is missing or malformed canonical fields: "
            f"{missing}. This is a Claude Code schema drift — the session "
            "cannot start without SessionStarted anchoring it."
        )

    return [
        SessionStarted(
            session_id=session_id,
            model=model,
            cwd=cwd,
            claude_code_version=version,
            tools_available=tools,
        )
    ]


def _handle_assistant(session_id: str, payload: dict) -> list[MarsEventBase]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    message_id = message.get("id") if isinstance(message.get("id"), str) else None
    content = message.get("content")
    if not isinstance(content, list):
        return []

    out: list[MarsEventBase] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if not isinstance(text, str):
                continue
            out.append(
                AssistantText(
                    session_id=session_id,
                    text=text,
                    message_id=message_id,
                    block_index=block_index,
                )
            )
        elif btype == "tool_use":
            tool_use_id = block.get("id")
            tool_name = block.get("name")
            if not isinstance(tool_use_id, str) or not isinstance(tool_name, str):
                continue
            raw_input = block.get("input")
            if isinstance(raw_input, dict):
                tool_input: dict = raw_input
            else:
                # Schema drift: runtime should always emit input as a dict.
                # Log a warning but still emit the tool call with {} so the
                # session makes forward progress. Story 1.3 will add a
                # structured warning callback.
                _log.warning(
                    "tool_use.input is not a dict (got %s) for tool %r; "
                    "emitting ToolCall with empty input.",
                    type(raw_input).__name__,
                    tool_name,
                )
                tool_input = {}
            out.append(
                ToolCall(
                    session_id=session_id,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    input=tool_input,
                    message_id=message_id,
                    block_index=block_index,
                )
            )
        # thinking blocks + unknown block types: dropped in v1.2
    return out


def _handle_user(session_id: str, payload: dict) -> list[MarsEventBase]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    message_id = message.get("id") if isinstance(message.get("id"), str) else None
    content = message.get("content")
    if not isinstance(content, list):
        return []

    out: list[MarsEventBase] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            continue
        raw_content = block.get("content", "")
        content_str = _coerce_tool_result_content(raw_content, tool_use_id)
        # Strict bool: only the literal JSON boolean `true` maps to an
        # error result. "false", 1, and other truthy values are NOT errors.
        raw_is_error = block.get("is_error", False)
        is_error = raw_is_error is True
        out.append(
            ToolResult(
                session_id=session_id,
                tool_use_id=tool_use_id,
                content=content_str,
                is_error=is_error,
                message_id=message_id,
                block_index=block_index,
            )
        )
    return out


def _coerce_tool_result_content(raw: object, tool_use_id: str) -> str:
    """Collapse a tool_result's ``content`` field into a single string.

    The runtime emits either a plain string or a list of content parts
    (``{"type":"text","text":"..."}`` and friends). v1.2 joins text parts
    best-effort. If *all* parts are non-text (e.g. image-only output) we
    emit a placeholder sentinel rather than an empty string so the
    supervisor can see in logs that content was lost. Story 1.3 will add
    structured block support so images survive.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        non_text_count = 0
        for part in raw:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            non_text_count += 1
        if non_text_count and not parts:
            _log.warning(
                "tool_result (tool_use_id=%s) has %d non-text part(s) and "
                "no text parts; v1.2 emits a placeholder. Story 1.3 adds "
                "structured block support.",
                tool_use_id,
                non_text_count,
            )
            return f"[{non_text_count} non-text tool_result block(s) dropped]"
        if non_text_count:
            _log.warning(
                "tool_result (tool_use_id=%s) dropped %d non-text part(s).",
                tool_use_id,
                non_text_count,
            )
        return "".join(parts)
    return str(raw)


def _handle_result(session_id: str, payload: dict) -> list[MarsEventBase]:
    """Handles both ``result.success`` and ``result.error`` — same shape.

    Mars v1 collapses both into :class:`SessionEnded`; the ``stop_reason``
    field carries the distinction for downstream consumers. This matches
    Camtom's ``turn_completed`` + ``turn_error`` pair, except Mars keeps
    them on the session-level event because Claude Code emits ``result``
    only once per ``claude -p`` invocation.
    """
    result = payload.get("result")
    result_str = result if isinstance(result, str) else None

    stop_reason = payload.get("stop_reason")
    if payload.get("subtype") == "error" and not stop_reason:
        stop_reason = "error"

    return [
        SessionEnded(
            session_id=session_id,
            result=result_str,
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            duration_ms=_int_or_none(payload.get("duration_ms")),
            num_turns=_int_or_none(payload.get("num_turns")),
            total_cost_usd=_float_or_none(payload.get("total_cost_usd")),
            permission_denials=_denials_or_empty(payload.get("permission_denials")),
        )
    ]


def _int_or_none(value: object) -> int | None:
    """Strict int extraction — matches the strict numeric fields on events."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _float_or_none(value: object) -> float | None:
    """Strict float extraction — accepts ints losslessly."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _denials_or_empty(value: object) -> list[dict]:
    if isinstance(value, list):
        return [d for d in value if isinstance(d, dict)]
    return []


def _handle_drop(session_id: str, payload: dict) -> list[MarsEventBase]:
    """Intentionally drop this runtime event from the Mars stream."""
    return []


# ---------------------------------------------------------------------------
# Dispatch table — flat, NOT a state machine (see module docstring).
# ---------------------------------------------------------------------------

_DISPATCH: dict[tuple[str | None, str | None], Handler] = {
    ("system", "init"): _handle_system_init,
    ("system", "hook_started"): _handle_drop,
    ("system", "hook_response"): _handle_drop,
    ("assistant", None): _handle_assistant,
    ("user", None): _handle_user,
    ("result", "success"): _handle_result,
    ("result", "error"): _handle_result,
    ("rate_limit_event", None): _handle_drop,
}
