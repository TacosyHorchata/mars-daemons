"""RPC wire format between the broker and the worker.

Shape: one JSON object per line, over the worker's stdin/stdout pipes.

Messages:
    worker → broker:
        {"rpc": "chat_request", "id": int, "args": {system, messages, tools, model, max_tokens}}
        {"rpc": "event", "event": {...full event dict...}}   # forwarded to broker's stdout

    broker → worker:
        {"rpc": "chat_chunk",    "id": int, "chunk": {kind: "text_delta"|"tool_use", ...}}
        {"rpc": "chat_response", "id": int, "response": {...}, "stop_reason": str}
        {"rpc": "chat_error",    "id": int, "error": str, "type": str}
        {"rpc": "user_input",    "text": str}
        {"rpc": "eof"}                                       # no more user input

The broker always drives LLM calls through chat_stream(). It sends N
`chat_chunk` messages (text_delta or tool_use) as the stream progresses,
then exactly one terminal `chat_response` (or `chat_error`) marking the
end of the stream. Workers that only need the final Response consume
chunks until the terminal message arrives. Workers that stream chunks
to their clients yield them as they arrive.
"""

from __future__ import annotations

from typing import Any

from .llm_client import ChatChunk, Response, ToolCall


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


def chunk_to_wire(chunk: ChatChunk) -> dict[str, Any]:
    """Serialize a ChatChunk (minus final_response, which lives in the
    terminal chat_response RPC) for the chat_chunk wire message."""
    if chunk.kind == "text_delta":
        return {"kind": "text_delta", "text": chunk.text}
    if chunk.kind == "tool_use" and chunk.tool_call is not None:
        return {
            "kind": "tool_use",
            "tool_call": {
                "id": chunk.tool_call.id,
                "name": chunk.tool_call.name,
                "input": chunk.tool_call.input,
            },
        }
    return {"kind": chunk.kind}


def chunk_from_wire(data: dict[str, Any]) -> ChatChunk:
    """Deserialize a chat_chunk wire payload back into a ChatChunk."""
    kind = data.get("kind")
    if kind == "text_delta":
        return ChatChunk(kind="text_delta", text=data.get("text", ""))
    if kind == "tool_use":
        tc = data.get("tool_call") or {}
        return ChatChunk(
            kind="tool_use",
            tool_call=ToolCall(
                id=tc.get("id", ""),
                name=tc.get("name", ""),
                input=tc.get("input", {}),
            ),
        )
    return ChatChunk(kind=kind or "text_delta")
