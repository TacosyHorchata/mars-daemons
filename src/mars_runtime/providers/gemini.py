"""Google Gemini client — placeholder.

Registered so `infer_provider("gemini-...")` routes here and raises a
clear "not implemented" error instead of a KeyError. Adding the real
implementation = translate Anthropic content-block format ↔ Gemini
`Content`/`Part`/`FunctionCall` shape, similar to azure_openai.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import ChatChunk, LLMClient, Message, Response, ToolSpec, register


class GeminiClient:
    def __init__(self, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "Gemini provider is not implemented yet. "
            "Use `provider: anthropic` or `provider: azure_openai` in agent.yaml, "
            "or add the implementation in src/mars_runtime/llm_client/gemini.py."
        )

    def chat(  # pragma: no cover - unreachable
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        raise NotImplementedError

    def chat_stream(  # pragma: no cover - unreachable
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[ChatChunk]:
        raise NotImplementedError


def _factory(**kwargs: Any) -> LLMClient:
    return GeminiClient(**kwargs)


register("gemini", _factory, model_prefixes=["gemini"])
