"""LLM client surface — Protocol conformance + AnthropicClient translation."""

from __future__ import annotations

from typing import Any

import pytest

from mars_runtime.llm_client import AnthropicClient, LLMClient, Response, ToolCall


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text

    def model_dump(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict):
        self.id = id
        self.name = name
        self.input = input

    def model_dump(self) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


class _FakeMessage:
    def __init__(self, content: list, stop_reason: str):
        self.content = content
        self.stop_reason = stop_reason


class _FakeAnthropicSDK:
    def __init__(self, response):
        self._response = response
        self.last_create_kwargs: dict | None = None

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.last_create_kwargs = kwargs
        return self._response


def _make_client(sdk):
    client = AnthropicClient.__new__(AnthropicClient)
    client._client = sdk
    return client


def test_anthropic_client_parses_text_response():
    sdk = _FakeAnthropicSDK(_FakeMessage([_FakeTextBlock("hello world")], stop_reason="end_turn"))
    resp = _make_client(sdk).chat(
        system="sys",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[],
        model="claude-opus-4-5",
        max_tokens=4096,
    )

    assert isinstance(resp, Response)
    assert resp.text == "hello world"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"
    assert sdk.last_create_kwargs["max_tokens"] == 4096


def test_anthropic_client_parses_tool_use_response():
    sdk = _FakeAnthropicSDK(
        _FakeMessage(
            [
                _FakeTextBlock("let me check"),
                _FakeToolUseBlock("tu_1", "read", {"file_path": "/x"}),
            ],
            stop_reason="tool_use",
        )
    )
    resp = _make_client(sdk).chat(
        system="sys", messages=[], tools=[], model="claude-opus-4-5", max_tokens=4096
    )

    assert resp.text == "let me check"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.id == "tu_1"
    assert tc.name == "read"
    assert tc.input == {"file_path": "/x"}
    assert resp.stop_reason == "tool_use"


def test_anthropic_client_passes_tools_none_when_empty():
    sdk = _FakeAnthropicSDK(_FakeMessage([_FakeTextBlock("ok")], stop_reason="end_turn"))
    _make_client(sdk).chat(system="s", messages=[], tools=[], model="m", max_tokens=1024)
    assert sdk.last_create_kwargs["tools"] is None


def test_anthropic_client_is_llmclient_protocol():
    client = _make_client(_FakeAnthropicSDK(_FakeMessage([], "end_turn")))
    assert isinstance(client, LLMClient)
