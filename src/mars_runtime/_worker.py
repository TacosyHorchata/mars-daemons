"""Worker process — runs the agent loop without LLM credentials in memory.

Spawned by `__main__.py` (the broker). Communicates with the broker over
stdin/stdout via the JSON-line RPC protocol in `_rpc.py`.

The worker never touches ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, or any
other LLM-provider secret. Those live only in the broker's memory. This
worker imports no provider SDK clients; every `chat()` call is proxied
to the broker.

Invocation (internal, not user-facing):
    python -m mars_runtime._worker
        --agent-json <json-encoded AgentConfig>
        --session-id <sess_*>
        --data-dir <abs path>
        [--start-messages-file <path to json>]
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from pathlib import Path

from . import events, session_store
from ._rpc import response_from_dict
from .agent import run
from .llm_client import LLMClient, Message, Response, ToolSpec
from .schema import AgentConfig
from .tools import ToolRegistry, load_all


class BrokerDisconnected(RuntimeError):
    pass


def _interpret_payload(payload: dict | None) -> Response:
    # `assert` disappears under `python -O`; use an explicit exception so
    # the protocol contract is enforced in all runtime modes.
    if payload is None:
        raise RuntimeError("broker returned empty payload")
    if payload.get("rpc") == "chat_error":
        err = payload.get("error", "unknown")
        if payload.get("type") == "BrokerDisconnected":
            raise BrokerDisconnected(err)
        raise RuntimeError(f"broker chat error: {err}")
    return response_from_dict(payload["response"])


class _BrokerLLMClient:
    """LLMClient proxy. Every chat() call round-trips to the broker.

    Tolerates pre-delivery: the RPC reader may receive a chat_response
    before chat() reserves its id (tests pre-script responses into stdin).

    If the broker disappears mid-chat (pipe closed, process crashed), all
    pending waiters are failed with BrokenBroker via `fail_all_pending()`
    so tools never hang forever.
    """

    def __init__(self, writer: "_RPCWriter") -> None:
        self._writer = writer
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, dict | None]] = {}
        self._lock = threading.Lock()
        self._disconnected = False

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1

            pre_delivered = req_id in self._pending and self._pending[req_id][1] is not None
            if pre_delivered:
                # Response already arrived before chat() was called.
                # Consume it without round-tripping.
                _, payload = self._pending.pop(req_id)
                return _interpret_payload(payload)

            if self._disconnected:
                raise BrokerDisconnected("broker is gone; cannot send chat request")

            if req_id in self._pending:
                ev, _ = self._pending[req_id]
            else:
                ev = threading.Event()
                self._pending[req_id] = (ev, None)

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

        ev.wait()
        with self._lock:
            _, payload = self._pending.pop(req_id)
        return _interpret_payload(payload)

    def _deliver(self, req_id: int, payload: dict) -> None:
        """Called by the RPC reader when a response lands."""
        with self._lock:
            if req_id in self._pending:
                ev, _ = self._pending[req_id]
                self._pending[req_id] = (ev, payload)
            else:
                ev = threading.Event()
                self._pending[req_id] = (ev, payload)
            ev.set()

    def fail_all_pending(self, reason: str) -> None:
        """Wake every still-waiting chat() with a synthetic broker-disconnected error.

        Entries that already have a delivered payload are left alone so a
        response that landed just before the pipe closed is not clobbered.
        """
        with self._lock:
            self._disconnected = True
            for req_id, (ev, payload) in list(self._pending.items()):
                if payload is not None:
                    continue
                self._pending[req_id] = (
                    ev,
                    {"rpc": "chat_error", "id": req_id, "error": reason, "type": "BrokerDisconnected"},
                )
                ev.set()


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


def _user_input_stream(q: queue.Queue) -> "object":
    """Yields user input lines from the queue until EOF sentinel (None)."""
    while True:
        item = q.get()
        if item is None:
            return
        yield item


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
        if kind in ("chat_response", "chat_error"):
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
    from . import agent as _agent_mod
    _agent_mod.emit = _emit_via_rpc  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mars_runtime._worker")
    parser.add_argument("--agent-json", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start-messages-file", default=None)
    args = parser.parse_args(argv)

    config = AgentConfig(**json.loads(args.agent_json))
    data_dir = Path(args.data_dir)
    workspace_path = data_dir / "workspace"
    sessions_dir = data_dir / "sessions"

    start_messages: list[Message] | None = None
    if args.start_messages_file:
        start_messages_path = Path(args.start_messages_file)
        start_messages = json.loads(start_messages_path.read_text(encoding="utf-8"))
        # Temp file is a duplicate of sessions/<id>.json; delete once consumed.
        try:
            start_messages_path.unlink()
        except OSError:
            pass

    writer = _RPCWriter()
    _install_event_forwarder(writer)

    broker_client = _BrokerLLMClient(writer)
    user_queue: queue.Queue = queue.Queue()
    shutdown = threading.Event()

    reader = threading.Thread(
        target=_stdin_reader,
        args=(broker_client, user_queue, shutdown),
        daemon=True,
    )
    reader.start()

    os.chdir(workspace_path)
    load_all()
    tools = ToolRegistry(config.tools or None)

    import subprocess as _sp

    try:
        run(
            config,
            broker_client,  # type: ignore[arg-type]  # satisfies LLMClient Protocol
            tools,
            turn_source=_user_input_stream(user_queue),
            sessions_dir=sessions_dir,
            session_id=args.session_id,
            workspace_path=workspace_path,
            start_messages=start_messages,
        )
    except KeyboardInterrupt:
        return 130
    except _sp.CalledProcessError as e:
        # Git exited non-zero — surface concisely instead of a traceback.
        print(f"git error: exit {e.returncode} running {e.cmd}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"persistence error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
