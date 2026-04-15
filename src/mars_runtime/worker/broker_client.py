"""Worker-side LLM proxy — forwards every chat() call to the broker over RPC.

Has no provider SDK imports and never sees API keys. Every call returns
to the broker (via stdout/stdin RPC); the broker is the only process
that owns credentials.

Tolerates pre-delivery: the RPC reader may drop items into a queue for
an id before chat_stream() reserves that id (tests pre-script responses
into stdin). We detect a non-empty queue at request time and serve from
it without sending a fresh chat_request.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator

from .._rpc import chunk_from_wire, response_from_dict
from ..llm_client import ChatChunk, Message, Response, ToolSpec


class BrokerDisconnected(RuntimeError):
    pass


class _BrokerLLMClient:
    def __init__(self, writer) -> None:
        self._writer = writer
        self._next_id = 0
        self._pending: dict[int, "queue.Queue"] = {}
        self._lock = threading.Lock()
        self._disconnected = False

    def _queue_for(self, req_id: int) -> "queue.Queue":
        with self._lock:
            q = self._pending.get(req_id)
            if q is None:
                q = queue.Queue()
                self._pending[req_id] = q
            return q

    def chat_stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[ChatChunk]:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            existing_q = self._pending.get(req_id)
            pre_delivered = existing_q is not None and not existing_q.empty()
            disconnected = self._disconnected
            if not pre_delivered and disconnected:
                raise BrokerDisconnected("broker is gone; cannot send chat request")
            if existing_q is None:
                existing_q = queue.Queue()
                self._pending[req_id] = existing_q
        q = existing_q

        if not pre_delivered:
            self._writer.send(
                {
                    "rpc": "chat_request",
                    "id": req_id,
                    "args": {
                        "system": system,
                        "messages": messages,
                        "tools": tools,
                        "model": model,
                        "max_tokens": max_tokens,
                    },
                }
            )

        try:
            while True:
                item = q.get()
                if isinstance(item, Exception):
                    raise item
                yield item
                if item.kind == "message_stop":
                    break
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        final: Response | None = None
        for chunk in self.chat_stream(
            system=system, messages=messages, tools=tools,
            model=model, max_tokens=max_tokens,
        ):
            if chunk.kind == "message_stop":
                final = chunk.final_response
        if final is None:
            raise RuntimeError("broker closed stream without final response")
        return final

    def _deliver(self, req_id: int, payload: dict) -> None:
        """Called by the RPC reader thread when a broker → worker message
        lands. Translates wire payloads into queue items consumed by an
        in-flight (or future) chat_stream()."""
        q = self._queue_for(req_id)
        rpc = payload.get("rpc")
        if rpc == "chat_chunk":
            q.put(chunk_from_wire(payload.get("chunk") or {}))
        elif rpc == "chat_response":
            resp_dict = payload.get("response")
            final = response_from_dict(resp_dict) if resp_dict else None
            q.put(
                ChatChunk(
                    kind="message_stop",
                    stop_reason=payload.get("stop_reason"),
                    final_response=final,
                )
            )
        elif rpc == "chat_error":
            err_type = payload.get("type")
            err_msg = payload.get("error", "unknown")
            if err_type == "BrokerDisconnected":
                q.put(BrokerDisconnected(err_msg))
            else:
                q.put(RuntimeError(f"broker chat error: {err_msg}"))

    def fail_all_pending(self, reason: str) -> None:
        """Unblock every still-waiting chat_stream() with a synthetic
        disconnect error. Pre-delivered queues keep their existing items
        (we just append the error behind them) so valid responses that
        landed before the pipe closed are still served."""
        with self._lock:
            self._disconnected = True
            for q in list(self._pending.values()):
                q.put(BrokerDisconnected(reason))
