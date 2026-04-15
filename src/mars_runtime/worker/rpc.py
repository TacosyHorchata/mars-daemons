"""Worker-side RPC plumbing — stdout writer + stdin reader thread +
event emitter redirect so agent events become RPC messages."""

from __future__ import annotations

import json
import queue
import sys
import threading

from .. import events
from .broker_client import _BrokerLLMClient


class _RPCWriter:
    """Serializes JSON-line writes to the worker's stdout."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._out = sys.stdout

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, default=str) + "\n"
        with self._lock:
            self._out.write(line)
            self._out.flush()


def _stdin_reader(
    broker_client: _BrokerLLMClient,
    user_queue: queue.Queue,
    shutdown: threading.Event,
) -> None:
    """Reads RPC messages from stdin (the broker) and dispatches.

    "eof" means the user has no more turns; we put None into the queue
    so the agent loop unblocks. But the reader keeps running to service
    chat_response messages for any in-flight chat request (the final
    turn's LLM call completes AFTER eof typically).
    """
    eof_signalled = False
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            # Broker must always send JSON objects. Drop arrays/scalars.
            continue

        kind = msg.get("rpc")
        if kind in ("chat_chunk", "chat_response", "chat_error"):
            req_id = msg["id"]
            broker_client._deliver(req_id, msg)
        elif kind == "user_input":
            user_queue.put(msg.get("text", ""))
        elif kind == "eof":
            if not eof_signalled:
                user_queue.put(None)
                eof_signalled = True
            # Keep reading — in-flight chat responses may still arrive.

    if not eof_signalled:
        # Broker closed stdin without an explicit eof RPC.
        user_queue.put(None)

    # Broker's stdin pipe closed → broker is gone. Fail every in-flight
    # chat() request so tool-side waiters never hang indefinitely.
    broker_client.fail_all_pending("broker closed the RPC pipe")
    shutdown.set()


def _install_event_forwarder(writer: _RPCWriter) -> None:
    """Replace events.emit so events become RPC messages, not raw stdout writes."""

    def _emit_via_rpc(event_type: str, **fields) -> None:
        from datetime import datetime, timezone
        event = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        writer.send({"rpc": "event", "event": event})

    # Monkey-patch at module level. agent.py imports `emit` via
    # `from .events import emit`, which binds to the original function
    # reference — so also replace the reference agent.py uses.
    events.emit = _emit_via_rpc  # type: ignore[assignment]
    from .. import agent as _agent_mod
    _agent_mod.emit = _emit_via_rpc  # type: ignore[assignment]
