"""Anthropic streaming — translation from SDK events to canonical ChatChunks.

Mocks the anthropic SDK's messages.stream() context manager. No network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from mars_runtime.providers import ChatChunk
from mars_runtime.providers.anthropic import AnthropicClient


# --- Fake event factories (match anthropic SDK event shape minimally) ------


def _text_start():
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="text"),
    )


def _text_delta(text: str):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_use_start(tid: str, name: str):
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="tool_use", id=tid, name=name),
    )


def _tool_input_delta(partial: str):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def _block_stop():
    return SimpleNamespace(type="content_block_stop")


def _message_delta(stop_reason: str):
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
    )


class _FakeStreamCtx:
    """Mock of `anthropic.Anthropic().messages.stream(...)` context manager."""

    def __init__(self, events: list[Any]):
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(self._events)


class _FakeAnthropicSDK:
    def __init__(self, events: list[Any]):
        self._events = events
        self.last_stream_kwargs: dict | None = None

    @property
    def messages(self):
        return self

    def stream(self, **kwargs):
        self.last_stream_kwargs = kwargs
        return _FakeStreamCtx(self._events)


def _make_client(sdk) -> AnthropicClient:
    c = AnthropicClient.__new__(AnthropicClient)
    c._client = sdk
    return c


# --- Tests ------------------------------------------------------------------


def test_stream_text_only_yields_deltas_and_final_stop():
    events = [
        _text_start(),
        _text_delta("Hola"),
        _text_delta(" mundo"),
        _block_stop(),
        _message_delta("end_turn"),
    ]
    client = _make_client(_FakeAnthropicSDK(events))

    chunks = list(
        client.chat_stream(
            system="sys", messages=[], tools=[], model="claude-opus-4-5", max_tokens=100,
        )
    )

    kinds = [c.kind for c in chunks]
    assert kinds == ["text_delta", "text_delta", "message_stop"]
    assert chunks[0].text == "Hola"
    assert chunks[1].text == " mundo"

    final = chunks[-1]
    assert final.stop_reason == "end_turn"
    assert final.final_response is not None
    assert final.final_response.text == "Hola mundo"
    assert final.final_response.tool_calls == []
    assert final.final_response.raw_content == [{"type": "text", "text": "Hola mundo"}]


def test_stream_tool_use_assembled_from_input_deltas():
    events = [
        _text_start(),
        _text_delta("let me check"),
        _block_stop(),
        _tool_use_start("tu_1", "read"),
        _tool_input_delta('{"file'),
        _tool_input_delta('_path": "/x"}'),
        _block_stop(),
        _message_delta("tool_use"),
    ]
    client = _make_client(_FakeAnthropicSDK(events))

    chunks = list(
        client.chat_stream(
            system="", messages=[], tools=[], model="m", max_tokens=100,
        )
    )

    # Expected: 1 text_delta, 1 tool_use, 1 message_stop
    assert [c.kind for c in chunks] == ["text_delta", "tool_use", "message_stop"]

    tool_chunk = chunks[1]
    assert tool_chunk.tool_call.id == "tu_1"
    assert tool_chunk.tool_call.name == "read"
    assert tool_chunk.tool_call.input == {"file_path": "/x"}

    final = chunks[2].final_response
    assert final.stop_reason == "tool_use"
    assert len(final.tool_calls) == 1
    assert final.raw_content == [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"file_path": "/x"}},
    ]


def test_stream_malformed_tool_json_preserves_raw():
    """If the model emits broken JSON for tool input, surface it for
    the tool to error out on — don't swallow."""
    events = [
        _tool_use_start("tu_1", "bash"),
        _tool_input_delta('{"command": "ls'),
        # No closing bracket — malformed.
        _block_stop(),
        _message_delta("tool_use"),
    ]
    client = _make_client(_FakeAnthropicSDK(events))

    chunks = list(
        client.chat_stream(system="", messages=[], tools=[], model="m", max_tokens=10)
    )
    tool_chunk = [c for c in chunks if c.kind == "tool_use"][0]
    assert "__malformed_input__" in tool_chunk.tool_call.input


def test_stream_empty_content_still_yields_message_stop():
    """Edge case: API returned nothing. We still emit one final chunk."""
    events = [_message_delta("end_turn")]
    client = _make_client(_FakeAnthropicSDK(events))

    chunks = list(
        client.chat_stream(system="", messages=[], tools=[], model="m", max_tokens=10)
    )
    assert len(chunks) == 1
    assert chunks[0].kind == "message_stop"
    assert chunks[0].final_response.text == ""


def test_stream_passes_kwargs_to_sdk():
    """Verify the SDK stream() is called with the right arguments."""
    events = [_message_delta("end_turn")]
    sdk = _FakeAnthropicSDK(events)
    client = _make_client(sdk)

    list(client.chat_stream(
        system="you are helpful",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[{"name": "read", "description": "", "input_schema": {}}],
        model="claude-sonnet-4-5",
        max_tokens=2048,
    ))

    kw = sdk.last_stream_kwargs
    assert kw["model"] == "claude-sonnet-4-5"
    assert kw["system"] == "you are helpful"
    assert kw["max_tokens"] == 2048
    assert len(kw["tools"]) == 1


def test_fallback_chat_stream_yields_single_message_stop():
    """Providers that don't implement streaming should fall back cleanly."""
    from mars_runtime.providers import Response, ToolCall, fallback_chat_stream

    class _SyncOnlyClient:
        def chat(self, **_):
            return Response(
                text="hi",
                tool_calls=[ToolCall(id="tu_1", name="read", input={"f": "/x"})],
                stop_reason="end_turn",
                raw_content=[
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"f": "/x"}},
                ],
            )

    chunks = list(
        fallback_chat_stream(
            _SyncOnlyClient(),
            system="", messages=[], tools=[], model="m", max_tokens=1,
        )
    )
    kinds = [c.kind for c in chunks]
    assert "tool_use" in kinds
    assert "text_delta" in kinds
    assert kinds[-1] == "message_stop"
    assert chunks[-1].final_response.text == "hi"


def test_client_satisfies_llmclient_protocol():
    """AnthropicClient must still satisfy the Protocol now that chat_stream exists."""
    from mars_runtime.providers import LLMClient
    client = _make_client(_FakeAnthropicSDK([_message_delta("end_turn")]))
    assert isinstance(client, LLMClient)
