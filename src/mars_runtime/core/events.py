"""Event publishing layer + SSE sink.

Durable events: mutate state -> allocate sequence -> store.save() -> sink.emit()
Ephemeral events: sink.emit() only — best-effort on reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .store import ConversationContext, PersistedState, UsageMetrics, get_store

logger = logging.getLogger(__name__)

# ─── Event type constants ──────────────────────────────────────────

EVENT_AGENT_MESSAGE = "agent_message"
EVENT_AGENT_CHUNK = "agent_chunk"
EVENT_AGENT_REASONING = "agent_reasoning"
EVENT_TOOL_STARTED = "tool_started"
EVENT_TOOL_COMPLETED = "tool_completed"
EVENT_TOOL_ERROR = "tool_error"
EVENT_CONVERSATION_STATE = "conversation_state"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_TURN_ERROR = "turn_error"

DURABLE_EVENTS = {
    EVENT_AGENT_MESSAGE,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_COMPLETED,
    EVENT_TOOL_ERROR,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_ERROR,
}

EPHEMERAL_EVENTS = {
    EVENT_AGENT_CHUNK,
    EVENT_AGENT_REASONING,
}

_DEFAULT_MAX_QUEUE_SIZE = 100


# ─── EventSink protocol + SSE implementation ──────────────────────

@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: dict) -> None: ...
    async def emit_chunk(self, conversation_id: str, delta: str) -> None: ...


class SSEEventSink:
    """In-process async queue sink for SSE streaming."""

    def __init__(self, max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._queues: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, conversation_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._queues.setdefault(conversation_id, []).append(queue)
        return queue

    def unsubscribe(self, conversation_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._queues.get(conversation_id)
        if subscribers is None:
            return
        try:
            subscribers.remove(queue)
        except ValueError:
            pass
        if not subscribers:
            self._queues.pop(conversation_id, None)

    async def emit(self, event: dict) -> None:
        conversation_id = event.get("conversation_id")
        if not conversation_id:
            return
        subscribers = self._queues.get(conversation_id)
        if not subscribers:
            return
        for queue in list(subscribers):
            _enqueue_with_durable_preference(queue, event)

    async def emit_chunk(self, conversation_id: str, delta: str) -> None:
        event = {
            "conversation_id": conversation_id,
            "type": "agent_chunk",
            "delta": delta,
        }
        await self.emit(event)


def _is_durable_event(event: dict[str, Any]) -> bool:
    return "sequence" in event


def _enqueue_with_durable_preference(queue: asyncio.Queue, event: dict[str, Any]) -> None:
    """Enqueue with durable event preference on overflow.

    When the queue is full, drain it, remove one ephemeral event (or the oldest
    durable if all are durable), re-enqueue the survivors, then add the new event.
    This avoids accessing asyncio.Queue private internals.
    """
    if not queue.full():
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
        return

    incoming_is_durable = _is_durable_event(event)

    # Drain the queue into a list (public API only)
    pending: list[dict] = []
    while not queue.empty():
        try:
            pending.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    # Find an ephemeral event to evict
    evict_idx = next(
        (i for i, existing in enumerate(pending) if not _is_durable_event(existing)),
        None,
    )

    if evict_idx is not None:
        pending.pop(evict_idx)
    elif incoming_is_durable:
        # All pending are durable — evict oldest 2 and insert gap marker
        logger.warning(
            "Durable event overflow — inserting gap marker: "
            "conversation_id=%s incoming_sequence=%s",
            event.get("conversation_id", "unknown"),
            event.get("sequence", "none"),
        )
        pending = pending[min(2, len(pending)):]
        pending.append({
            "type": "_gap",
            "conversation_id": event.get("conversation_id"),
            "reason": "durable_overflow",
        })
    else:
        # Incoming is ephemeral and all pending are durable — drop the incoming
        for item in pending:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                break
        return

    # Re-enqueue survivors + new event
    pending.append(event)
    for item in pending:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            if _is_durable_event(item):
                logger.warning(
                    "Dropping event after queue overflow: conversation_id=%s type=%s sequence=%s",
                    item.get("conversation_id", "unknown"),
                    item.get("type", "unknown"),
                    item.get("sequence", "none"),
                )
            break


# ─── Sequence counter ──────────────────────────────────────────────

def next_sequence(state: dict) -> int:
    state["_event_sequence"] = state.get("_event_sequence", 0) + 1
    return state["_event_sequence"]


# ─── Publishing functions ──────────────────────────────────────────

async def publish_durable_event(
    conversation_id: str,
    event_type: str,
    state: dict,
    **kwargs,
) -> None:
    event = {
        "conversation_id": conversation_id,
        "type": event_type,
        "sequence": next_sequence(state),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    durable_events = state.setdefault("_durable_events", [])
    durable_events.append(event)

    store = get_store()
    persisted = PersistedState(
        context=ConversationContext(
            messages=state["messages"],
            tool_calls=state["tool_calls"],
            conversation=state["conversation"],
            scratchpad=state["scratchpad"],
            files=state["files"],
            system_prompt=state.get("system_prompt"),
            active_skills=state.get("active_skills", []),
            _event_sequence=state.get("_event_sequence", 0),
            _durable_events=state.get("_durable_events", []),
        ),
        status=state["status"],
        usage=state.get("usage", UsageMetrics()),
        last_message_at=datetime.now(timezone.utc).isoformat(),
    )
    org_id = state.get("org_id", "")
    await store.save(conversation_id, persisted, org_id=org_id)

    sink = get_sink()
    await sink.emit(event)


async def publish_ephemeral(
    conversation_id: str,
    event_type: str,
    state: dict,
    **kwargs,
) -> None:
    event = {
        "conversation_id": conversation_id,
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    sink = get_sink()
    await sink.emit(event)


async def publish_chunk(conversation_id: str, delta: str) -> None:
    sink = get_sink()
    await sink.emit_chunk(conversation_id, delta)


# ─── Module-level singleton ────────────────────────────────────────

_sink: EventSink | None = None


def get_sink() -> EventSink:
    global _sink
    if _sink is None:
        _sink = SSEEventSink()
    return _sink


def set_sink(sink: EventSink) -> None:
    global _sink
    _sink = sink


def reset_sink() -> None:
    global _sink
    if _sink is not None:
        close_fn = getattr(_sink, "close", None)
        if callable(close_fn):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                loop.create_task(close_fn())
            else:
                try:
                    asyncio.run(close_fn())
                except RuntimeError:
                    pass
    _sink = None
