"""OpenAI/Azure streaming — delta accumulation tests.

OpenAI tool_calls arrive as deltas keyed by `index`. `id` usually only
on the first delta for a slot; `function.name` arrives complete on the
first delta; `function.arguments` is a JSON string that accumulates
across chunks. These tests mock that shape and verify the accumulator
produces one well-formed ToolCall per slot.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mars_runtime.llm_client.azure_openai import _stream_translate


# --- Fake chunk factory ----------------------------------------------------


def _chunk(content=None, tool_call_deltas=None, finish_reason=None):
    """Build a fake ChatCompletionChunk the way the openai SDK yields them."""
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_call_deltas,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _tc(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


# --- Tests -----------------------------------------------------------------


def test_stream_text_only():
    stream = iter([
        _chunk(content="Hola"),
        _chunk(content=" mundo"),
        _chunk(finish_reason="stop"),
    ])
    chunks = list(_stream_translate(stream))
    kinds = [c.kind for c in chunks]
    assert kinds == ["text_delta", "text_delta", "message_stop"]
    assert chunks[0].text == "Hola"
    assert chunks[1].text == " mundo"
    final = chunks[-1].final_response
    assert final.text == "Hola mundo"
    assert final.stop_reason == "end_turn"


def test_stream_tool_use_accumulates_across_deltas():
    """name arrives on first delta, arguments JSON builds up over several."""
    stream = iter([
        _chunk(content="let me check"),
        _chunk(tool_call_deltas=[_tc(0, id="call_1", name="read", arguments='{"file')]),
        _chunk(tool_call_deltas=[_tc(0, arguments='_path"')]),
        _chunk(tool_call_deltas=[_tc(0, arguments=': "/x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    chunks = list(_stream_translate(stream))
    # text_delta, tool_use, message_stop
    assert [c.kind for c in chunks] == ["text_delta", "tool_use", "message_stop"]
    tc = chunks[1].tool_call
    assert tc.id == "call_1"
    assert tc.name == "read"
    assert tc.input == {"file_path": "/x"}
    final = chunks[-1].final_response
    assert final.stop_reason == "tool_use"


def test_stream_multiple_tool_calls_interleaved():
    """OpenAI can split tool_call deltas across chunks by index."""
    stream = iter([
        _chunk(tool_call_deltas=[_tc(0, id="call_a", name="read", arguments='{"x":')]),
        _chunk(tool_call_deltas=[_tc(1, id="call_b", name="list", arguments='{"path":"/"')]),
        _chunk(tool_call_deltas=[_tc(0, arguments='1}')]),
        _chunk(tool_call_deltas=[_tc(1, arguments='}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    chunks = list(_stream_translate(stream))
    tool_chunks = [c for c in chunks if c.kind == "tool_use"]
    assert len(tool_chunks) == 2
    # Sorted by index — index 0 first
    assert tool_chunks[0].tool_call.id == "call_a"
    assert tool_chunks[0].tool_call.name == "read"
    assert tool_chunks[0].tool_call.input == {"x": 1}
    assert tool_chunks[1].tool_call.id == "call_b"
    assert tool_chunks[1].tool_call.name == "list"
    assert tool_chunks[1].tool_call.input == {"path": "/"}


def test_stream_malformed_arguments_preserved():
    stream = iter([
        _chunk(tool_call_deltas=[_tc(0, id="call_x", name="bash", arguments='{"command":"ls')]),
        # never closes the JSON
        _chunk(finish_reason="tool_calls"),
    ])
    chunks = list(_stream_translate(stream))
    tool = [c for c in chunks if c.kind == "tool_use"][0]
    assert "__malformed_arguments__" in tool.tool_call.input


def test_stream_content_filter_passes_through():
    """finish_reason=content_filter should NOT map to end_turn."""
    stream = iter([_chunk(finish_reason="content_filter")])
    chunks = list(_stream_translate(stream))
    assert chunks[-1].stop_reason == "content_filter"


def test_stream_empty_response_inserts_placeholder():
    """No content, no tool_calls — we still emit a valid final."""
    stream = iter([_chunk(finish_reason="stop")])
    chunks = list(_stream_translate(stream))
    final = chunks[-1].final_response
    assert final.text  # non-empty placeholder
    assert "empty response" in final.text
    # raw_content must not be empty
    assert final.raw_content


def test_stream_max_tokens_stop_reason():
    stream = iter([
        _chunk(content="truncat"),
        _chunk(content="ed..."),
        _chunk(finish_reason="length"),
    ])
    chunks = list(_stream_translate(stream))
    assert chunks[-1].stop_reason == "max_tokens"


def test_stream_openai_direct_client_uses_same_helper():
    """openai_direct.OpenAIClient.chat_stream must route through _stream_translate."""
    from mars_runtime.llm_client.openai_direct import OpenAIClient

    class _FakeChatCompletions:
        def __init__(self, stream): self._stream = stream
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return self._stream

    class _FakeSDK:
        def __init__(self, stream):
            self.chat = SimpleNamespace(completions=_FakeChatCompletions(stream))

    stream = iter([
        _chunk(content="hi"),
        _chunk(finish_reason="stop"),
    ])
    client = OpenAIClient.__new__(OpenAIClient)
    client._client = _FakeSDK(stream)

    chunks = list(client.chat_stream(
        system="", messages=[], tools=[], model="gpt-4o", max_tokens=10,
    ))
    assert chunks[0].kind == "text_delta"
    assert chunks[0].text == "hi"
    assert chunks[-1].kind == "message_stop"
