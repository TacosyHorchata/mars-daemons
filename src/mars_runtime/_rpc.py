"""RPC wire format between the broker and the worker.

Shape: one JSON object per line, over the worker's stdin/stdout pipes.

Messages:
    worker → broker:
        {"rpc": "chat_request", "id": int, "args": {system, messages, tools, model, max_tokens}}
        {"rpc": "event", "event": {...full event dict...}}   # forwarded to broker's stdout

    broker → worker:
        {"rpc": "chat_response", "id": int, "response": {text, tool_calls, stop_reason, raw_content}}
        {"rpc": "chat_error",    "id": int, "error": str, "type": str}
        {"rpc": "user_input",    "text": str}
        {"rpc": "eof"}                                       # no more user input

This module defines the encode/decode helpers so broker and worker do not
drift on the shape. Keep it boring — JSON only, no binary, no framing
beyond newlines.
"""

from __future__ import annotations

from typing import Any

from .llm_client import Response, ToolCall


def response_to_dict(resp: Response) -> dict[str, Any]:
    return {
        "text": resp.text,
        "tool_calls": [
            {"id": t.id, "name": t.name, "input": t.input} for t in resp.tool_calls
        ],
        "stop_reason": resp.stop_reason,
        "raw_content": resp.raw_content,
    }


def response_from_dict(data: dict[str, Any]) -> Response:
    return Response(
        text=data["text"],
        tool_calls=[ToolCall(**t) for t in data.get("tool_calls", [])],
        stop_reason=data.get("stop_reason"),
        raw_content=data.get("raw_content", []),
    )
