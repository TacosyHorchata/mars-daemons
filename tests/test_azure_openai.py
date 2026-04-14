"""Azure OpenAI client translation tests.

Mocks the openai SDK so these tests run without azure creds or network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from mars_runtime.llm_client.azure_openai import (
    AzureOpenAIClient,
    _from_openai_response,
    _to_openai_messages,
    _to_openai_tools,
)


# --- OpenAI → ours (response) ----------------------------------------------


def _openai_response(*, content: str | None = None, tool_calls: list | None = None, finish: str = "stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=message, finish_reason=finish)
    return SimpleNamespace(choices=[choice])


def _openai_tool_call(id: str, name: str, args: dict):
    return SimpleNamespace(
        id=id, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def test_from_response_plain_text():
    resp = _from_openai_response(_openai_response(content="hello", finish="stop"))
    assert resp.text == "hello"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"
    assert resp.raw_content == [{"type": "text", "text": "hello"}]


def test_from_response_tool_call():
    tc = _openai_tool_call("tu_1", "read", {"file_path": "/x"})
    resp = _from_openai_response(
        _openai_response(content=None, tool_calls=[tc], finish="tool_calls")
    )
    assert resp.text == ""
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "tu_1"
    assert resp.tool_calls[0].name == "read"
    assert resp.tool_calls[0].input == {"file_path": "/x"}
    assert resp.stop_reason == "tool_use"
    # raw_content stays Anthropic-shape for storage roundtrip
    assert resp.raw_content == [
        {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"file_path": "/x"}}
    ]


def test_from_response_mixed():
    tc = _openai_tool_call("tu_1", "bash", {"command": "ls"})
    resp = _from_openai_response(
        _openai_response(content="running ls", tool_calls=[tc], finish="tool_calls")
    )
    assert resp.text == "running ls"
    assert len(resp.tool_calls) == 1
    assert len(resp.raw_content) == 2
    assert resp.raw_content[0]["type"] == "text"
    assert resp.raw_content[1]["type"] == "tool_use"


def test_from_response_max_tokens():
    resp = _from_openai_response(_openai_response(content="truncated", finish="length"))
    assert resp.stop_reason == "max_tokens"


def test_from_response_malformed_tool_args():
    """Model returns garbage in function.arguments — preserve for surfacing."""
    bad_tc = SimpleNamespace(
        id="tu_1", type="function",
        function=SimpleNamespace(name="read", arguments="{not valid json"),
    )
    resp = _from_openai_response(
        _openai_response(tool_calls=[bad_tc], finish="tool_calls")
    )
    assert resp.tool_calls[0].input == {"__malformed_arguments__": "{not valid json"}


# --- ours → OpenAI (messages) ----------------------------------------------


def test_to_openai_messages_prepends_system():
    out = _to_openai_messages("you are helpful", [])
    assert out == [{"role": "system", "content": "you are helpful"}]


def test_to_openai_messages_user_text():
    out = _to_openai_messages(
        "sys",
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    )
    assert out[1] == {"role": "user", "content": "hi"}


def test_to_openai_messages_assistant_text_and_tool():
    out = _to_openai_messages(
        "",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"f": "/x"}},
                ],
            }
        ],
    )
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "let me check"
    assert out[0]["tool_calls"] == [
        {
            "id": "tu_1",
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"f": "/x"})},
        }
    ]


def test_to_openai_messages_tool_result_becomes_tool_message():
    out = _to_openai_messages(
        "",
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "result-text", "is_error": False},
                ],
            }
        ],
    )
    assert out[0] == {"role": "tool", "tool_call_id": "tu_1", "content": "result-text"}


def test_to_openai_messages_tool_result_content_list():
    """Anthropic allows tool_result content to be a list of blocks."""
    out = _to_openai_messages(
        "",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "block one"}, {"type": "text", "text": "block two"}],
                    }
                ],
            }
        ],
    )
    assert "block one" in out[0]["content"]
    assert "block two" in out[0]["content"]


def test_to_openai_tools_schema_shape():
    out = _to_openai_tools(
        [{"name": "read", "description": "Read a file", "input_schema": {"type": "object"}}]
    )
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {"type": "object"},
            },
        }
    ]


# --- Client end-to-end (mocked SDK) ----------------------------------------


class _FakeChatCompletions:
    def __init__(self, response: Any):
        self._response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeAzureSDK:
    def __init__(self, response: Any):
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(response))


def test_client_roundtrip_mocked_sdk(monkeypatch):
    """Verify the client wires translation IN and translation OUT."""
    fake_resp = _openai_response(content="reply", finish="stop")
    fake_sdk = _FakeAzureSDK(fake_resp)

    client = AzureOpenAIClient.__new__(AzureOpenAIClient)
    client._client = fake_sdk

    resp = client.chat(
        system="you are helpful",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[{"name": "read", "description": "Read", "input_schema": {}}],
        model="my-deployment",
        max_tokens=1024,
    )

    assert resp.text == "reply"
    # Verify translated request shape
    sent = fake_sdk.chat.completions.last_kwargs
    assert sent["model"] == "my-deployment"
    assert sent["max_tokens"] == 1024
    assert sent["messages"][0] == {"role": "system", "content": "you are helpful"}
    assert sent["messages"][1] == {"role": "user", "content": "hi"}
    assert sent["tools"][0]["type"] == "function"


def test_content_filter_preserves_reason_and_placeholder():
    """Filtered or otherwise empty Azure responses must not persist empty content."""
    resp = _from_openai_response(_openai_response(content=None, finish="content_filter"))
    assert resp.stop_reason == "content_filter"
    # raw_content must NOT be empty — session.json would be malformed
    assert len(resp.raw_content) == 1
    assert resp.raw_content[0]["type"] == "text"
    assert "empty response" in resp.raw_content[0]["text"]
    assert "content_filter" in resp.raw_content[0]["text"]


def test_empty_completion_gets_placeholder():
    """If content is None and no tool calls, synthesize a visible placeholder."""
    resp = _from_openai_response(_openai_response(content=None, finish="stop"))
    assert resp.raw_content  # not empty
    assert "empty response" in resp.text


def test_tool_result_is_error_encoded_in_content():
    """Anthropic is_error=True must survive the OpenAI tool-message round-trip."""
    out = _to_openai_messages(
        "",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "file not found",
                        "is_error": True,
                    }
                ],
            }
        ],
    )
    assert out[0]["role"] == "tool"
    assert "[tool error]" in out[0]["content"]
    assert "file not found" in out[0]["content"]


def test_unsupported_user_block_raises():
    with pytest.raises(ValueError, match="unsupported user content block"):
        _to_openai_messages(
            "",
            [{"role": "user", "content": [{"type": "image", "source": {"data": "..."}}]}],
        )


def test_unsupported_assistant_block_raises():
    with pytest.raises(ValueError, match="unsupported assistant content block"):
        _to_openai_messages(
            "",
            [{"role": "assistant", "content": [{"type": "image", "source": {"data": "..."}}]}],
        )


def test_client_raises_if_openai_not_installed(monkeypatch):
    """If `openai` isn't on the path, give a clear install hint."""
    import builtins
    real_import = builtins.__import__

    def _no_openai(name, *a, **kw):
        if name == "openai" or name.startswith("openai."):
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_openai)
    with pytest.raises(ImportError, match="uv sync"):
        AzureOpenAIClient()
