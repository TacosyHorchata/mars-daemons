"""Anthropic Messages API client.

Canonical format — no translation. The agent loop speaks Anthropic's
content-block shape natively; this client just forwards.

Env: ANTHROPIC_API_KEY (or pass api_key to constructor).
"""

from __future__ import annotations

from typing import Any

from .base import LLMClient, Message, Response, ToolCall, ToolSpec, register


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


def _factory(**kwargs: Any) -> LLMClient:
    return AnthropicClient(**kwargs)


register("anthropic", _factory, model_prefixes=["claude"])
