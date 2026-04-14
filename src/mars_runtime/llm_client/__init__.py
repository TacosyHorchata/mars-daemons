"""LLM provider registry + canonical protocol.

Usage:
    from mars_runtime.llm_client import load_all, get, Message, Response

    load_all()  # import all provider modules so they self-register
    llm = get("anthropic")  # or "azure_openai", "gemini"
"""

from __future__ import annotations

from .base import (
    LLMClient,
    Message,
    ProviderCollision,
    Response,
    ToolCall,
    ToolSpec,
    get,
    infer_provider,
    load_all,
    register,
    registered,
)

__all__ = [
    "LLMClient",
    "Message",
    "ProviderCollision",
    "Response",
    "ToolCall",
    "ToolSpec",
    "get",
    "infer_provider",
    "load_all",
    "register",
    "registered",
]
