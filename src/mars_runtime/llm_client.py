"""Provider-neutral LLM client.

The `LLMClient` protocol is the only interface `agent.py` knows. v0 ships
one concrete implementation (`AnthropicClient`). Adding OpenAI is a new
class that implements the same protocol — zero changes in `agent.py`.

Message shape uses Anthropic's content-block format (text + tool_use +
tool_result blocks). This is the richer format; OpenAI's function-calling
translates down trivially. Going the other way would need lossy
translation, so we standardize on the richer side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: list[dict]


class ToolSpec(TypedDict):
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str | None
    raw_content: list[dict] = field(default_factory=list)


@runtime_checkable
class LLMClient(Protocol):
    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response: ...


class AnthropicClient:
    def __init__(self, api_key: str | None = None):
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
            messages=messages,
            tools=tools or None,
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
