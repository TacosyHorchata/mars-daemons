"""OpenAI direct client (api.openai.com).

Shares the Anthropic ↔ OpenAI function-calling translation helpers from
`azure_openai.py` — same chat completions surface, only the SDK client
class differs. For Azure-hosted deployments use `provider: azure_openai`.

Env (read by the openai SDK):
    OPENAI_API_KEY
    OPENAI_ORGANIZATION (optional)

Model field in agent.yaml maps directly to the OpenAI model id
(e.g., "gpt-5.4", "gpt-4o", "o1-preview").

`openai>=1.0` is a required runtime dependency (shared with azure_openai).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .azure_openai import (
    _from_openai_response,
    _stream_translate,
    _to_openai_messages,
    _to_openai_tools,
)
from .base import ChatChunk, LLMClient, Message, Response, ToolSpec, register


class OpenAIClient:
    def __init__(self, **sdk_kwargs: Any) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "The openai provider needs `openai>=1.0` installed "
                "(it is a required runtime dependency; run `uv sync`)."
            ) from e

        self._client = OpenAI(**sdk_kwargs)

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        openai_messages = _to_openai_messages(system, messages)
        openai_tools = _to_openai_tools(tools)

        resp = self._client.chat.completions.create(
            model=model,
            messages=openai_messages,
            tools=openai_tools or None,
            max_tokens=max_tokens,
        )

        return _from_openai_response(resp)

    def chat_stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[ChatChunk]:
        openai_messages = _to_openai_messages(system, messages)
        openai_tools = _to_openai_tools(tools)

        sdk_stream = self._client.chat.completions.create(
            model=model,
            messages=openai_messages,
            tools=openai_tools or None,
            max_tokens=max_tokens,
            stream=True,
        )
        yield from _stream_translate(sdk_stream)


def _factory(**kwargs: Any) -> LLMClient:
    return OpenAIClient(**kwargs)


register("openai", _factory, model_prefixes=["gpt", "o1", "o3"])
