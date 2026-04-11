"""Parser: Claude Code ``stream-json`` → Mars events.

The supervisor spawns ``claude -p ... --output-format stream-json`` as a
subprocess and feeds its stdout through :func:`parse_stream` to produce an
async iterator of typed :class:`~events.types.MarsEventBase` payloads.

This module is the contract between the pinned, externally-owned Claude
Code CLI and the rest of Mars. It is the highest-risk file in the
runtime. ``tests/contract/test_claude_code_stream.py`` runs the real
pinned CLI against it so schema drift fails fast.

Supported runtime events
------------------------

* ``system.init`` → :class:`~events.types.SessionStarted` — canonical
  session anchor. Raises :class:`CriticalParseError` on malformed
  payloads because Mars cannot emit any other event without it.
* ``assistant`` → one :class:`~events.types.AssistantText` per ``text``
  content block + one :class:`~events.types.ToolCall` per ``tool_use``
  block. ``thinking`` blocks are dropped (internal model reasoning).
* ``user`` tool_result → :class:`~events.types.ToolResult` per block.
* ``result.success`` / ``result.error`` → :class:`~events.types.SessionEnded`.
* ``stream_event`` (only when the supervisor runs the CLI with
  ``--include-partial-messages``) → one :class:`~events.types.AssistantChunk`
  per ``content_block_delta.text_delta``. Every other inner event type
  (``message_start``, ``content_block_start``, ``content_block_stop``,
  ``message_delta``, ``message_stop``, ``input_json_delta``) is dropped.
  Chunks are ephemeral by design — see :mod:`events.types`.

Known runtime events that Mars intentionally drops
--------------------------------------------------

* ``rate_limit_event`` — quota / overage status, Mars exposes cost via
  :class:`SessionEnded` instead.
* ``system.hook_started`` / ``system.hook_response`` — user-global hook
  lifecycle; not present in clean Mars containers but tolerated here.
* Unknown ``(type, subtype)`` combinations — dropped silently at
  :func:`parse_line`; :func:`parse_stream` callers can observe drops via
  the ``on_warning`` callback.

Error surfaces
--------------

* :class:`json.JSONDecodeError` — raised from :func:`parse_line` when a
  line is malformed JSON; :func:`parse_stream` catches and reports via
  ``on_warning``.
* :class:`ParseError` — raised when a line decodes to something other
  than a JSON object; :func:`parse_stream` catches and reports.
* :class:`CriticalParseError` — raised only from ``system.init`` handler
  when the session root event is unusable. :func:`parse_stream`
  deliberately does NOT catch this: it propagates to the supervisor so
  the broken session can be killed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import AsyncIterator, Callable

from events.types import (
    AssistantChunk,
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


#: Callback invoked by :func:`parse_stream` whenever a line is dropped
#: because it failed to decode or map to a Mars event. Signature:
#: ``(message: str, exception: Exception | None) -> None``. Intentionally
#: narrower than ``BaseException`` so ``KeyboardInterrupt`` /
#: ``SystemExit`` never flow through the callback path.
WarningCallback = Callable[[str, "Exception | None"], None]


async def parse_stream(
    session_id: str,
    stdout: asyncio.StreamReader,
    *,
    on_warning: WarningCallback | None = None,
) -> AsyncIterator[MarsEventBase]:
    """Consume a JSONL byte stream and yield typed Mars events.

    The supervisor wires this to the stdout of a
    ``claude -p ... --output-format stream-json`` subprocess. Each line
    is decoded as UTF-8, stripped, and passed to :func:`parse_line`.

    Recoverable errors (:class:`json.JSONDecodeError`, :class:`ParseError`,
    unicode decode failure) are swallowed so one bad line does not kill
    the stream, but they are reported via ``on_warning`` when provided.

    Unrecoverable errors (:class:`CriticalParseError`, raised on a
    malformed ``system.init`` event) are NOT caught here — they
    propagate to the supervisor so it can kill the broken session
    instead of emitting orphan events without an anchor.

    Args:
        session_id: Mars-assigned session identifier, stamped on every
            emitted event.
        stdout: An :class:`asyncio.StreamReader` connected to the
            runtime subprocess's stdout. The stream must signal EOF
            (via ``feed_eof`` in tests or process exit in production)
            for the iterator to terminate.
        on_warning: Optional callback invoked for every dropped line
            with a short human message and the underlying exception
            (or ``None`` for soft drops like unicode failures). Narrower
            than ``BaseException`` on purpose — ``KeyboardInterrupt`` /
            ``SystemExit`` propagate normally.

    Yields:
        Typed Mars events in the order they arrive on the stream.

    Raises:
        CriticalParseError: if the runtime emits a malformed
            ``system.init`` event — the session cannot proceed.
    """
    while True:
        line_bytes = await stdout.readline()
        if not line_bytes:
            return
        try:
            decoded = line_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            if on_warning is not None:
                on_warning("unicode decode failure on stream line", exc)
            continue
        try:
            events = parse_line(session_id, decoded)
        except CriticalParseError:
            # Session anchor is malformed — caller (supervisor) must
            # kill the session and surface the error.
            raise
        except json.JSONDecodeError as exc:
            if on_warning is not None:
                on_warning(f"json decode error: {decoded.strip()[:120]!r}", exc)
            continue
        except ParseError as exc:
            if on_warning is not None:
                on_warning(f"parse error: {decoded.strip()[:120]!r}", exc)
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
    field carries the distinction for downstream consumers. The
    distinction is kept on the session-level event because Claude Code
    emits ``result`` only once per ``claude -p`` invocation — there is
    no per-turn granularity to preserve at this layer.
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


def _handle_stream_event(session_id: str, payload: dict) -> list[MarsEventBase]:
    """Translate Anthropic streaming SSE events (``--include-partial-messages``)
    into ephemeral :class:`AssistantChunk` events.

    Runtime shape (confirmed 2026-04-10 against Claude Code 2.1.101)::

        {"type":"stream_event","event":{"type":"content_block_delta",
          "index":0,"delta":{"type":"text_delta","text":"..."}}}

    Only ``content_block_delta`` with an inner ``text_delta`` delta
    produces output. Every other inner event type (``message_start``,
    ``content_block_start``, ``content_block_stop``, ``message_delta``,
    ``message_stop``, ``input_json_delta``) is dropped because the
    canonical :class:`AssistantText` / :class:`ToolCall` events emitted
    from the companion ``assistant`` message carry the authoritative
    state; chunks are a best-effort streaming UX layer.

    Chunks deliberately carry ``message_id=None`` — this parser is
    stateless (no state machine, per v1 design). Consumers correlate
    chunks to the subsequent :class:`AssistantText` by order.
    """
    inner = payload.get("event")
    if not isinstance(inner, dict):
        return []
    if inner.get("type") != "content_block_delta":
        return []
    delta = inner.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return []
    text = delta.get("text")
    if not isinstance(text, str) or not text:
        return []
    block_index = inner.get("index")
    # strict int check — matches AssistantChunk.block_index strict=True
    if isinstance(block_index, bool) or not isinstance(block_index, int) or block_index < 0:
        block_index = None
    return [
        AssistantChunk(
            session_id=session_id,
            delta=text,
            message_id=None,
            block_index=block_index,
        )
    ]


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
    ("stream_event", None): _handle_stream_event,
}
