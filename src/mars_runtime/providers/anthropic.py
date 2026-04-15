"""Anthropic Messages API client.

Canonical format — no translation. The agent loop speaks Anthropic's
content-block shape natively; this client just forwards.

Env: ANTHROPIC_API_KEY (or pass api_key to constructor).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from .base import (
    ChatChunk,
    LLMClient,
    Message,
    Response,
    ToolCall,
    ToolSpec,
    register,
)


class AnthropicClient:
    def __init__(self, api_key: str | None = None) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        resp = self._client.messages.create(
            model=model,
            system=system,
            messages=messages,  # type: ignore[arg-type]
            tools=tools or None,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict] = []

        for block in resp.content:
            block_dict = block.model_dump()
            raw.append(block_dict)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=dict(block.input))
                )

        return Response(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            raw_content=raw,
        )

    def chat_stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[ChatChunk]:
        """Stream events from Anthropic as canonical ChatChunks.

        Translates the SDK's typed event stream into our three-kind
        shape: text_delta (incremental), tool_use (complete block), and
        message_stop (final Response). Tool-use input arrives as
        `input_json_delta` strings; we accumulate until the block stops
        and parse once.
        """
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_content: list[dict] = []
        stop_reason: str | None = None

        current_block: dict[str, Any] | None = None

        with self._client.messages.stream(
            model=model,
            system=system,
            messages=messages,  # type: ignore[arg-type]
            tools=tools or None,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        ) as stream:
            for event in stream:
                et = event.type
                if et == "content_block_start":
                    cb = event.content_block
                    if cb.type == "text":
                        current_block = {"type": "text", "text": ""}
                    elif cb.type == "tool_use":
                        current_block = {
                            "type": "tool_use",
                            "id": cb.id,
                            "name": cb.name,
                            "input_raw": "",
                        }
                elif et == "content_block_delta":
                    if current_block is None:
                        continue
                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        current_block["text"] = current_block.get("text", "") + delta.text
                        text_parts.append(delta.text)
                        yield ChatChunk(kind="text_delta", text=delta.text)
                    elif dtype == "input_json_delta":
                        current_block["input_raw"] = (
                            current_block.get("input_raw", "") + delta.partial_json
                        )
                elif et == "content_block_stop":
                    if current_block is None:
                        continue
                    if current_block["type"] == "text":
                        raw_content.append(
                            {"type": "text", "text": current_block["text"]}
                        )
                    elif current_block["type"] == "tool_use":
                        raw = current_block.get("input_raw", "")
                        try:
                            input_obj = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            # Model emitted malformed JSON; preserve the
                            # raw string so the tool can surface an error.
                            input_obj = {"__malformed_input__": raw}
                        tc = ToolCall(
                            id=current_block["id"],
                            name=current_block["name"],
                            input=input_obj,
                        )
                        tool_calls.append(tc)
                        raw_content.append(
                            {
                                "type": "tool_use",
                                "id": current_block["id"],
                                "name": current_block["name"],
                                "input": input_obj,
                            }
                        )
                        yield ChatChunk(kind="tool_use", tool_call=tc)
                    current_block = None
                elif et == "message_delta":
                    sr = getattr(event.delta, "stop_reason", None)
                    if sr:
                        stop_reason = sr

        final = Response(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=raw_content,
        )
        yield ChatChunk(
            kind="message_stop",
            stop_reason=stop_reason,
            final_response=final,
        )


def _factory(**kwargs: Any) -> LLMClient:
    return AnthropicClient(**kwargs)


register("anthropic", _factory, model_prefixes=["claude"])
