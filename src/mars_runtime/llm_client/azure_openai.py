"""Azure OpenAI client.

Translates IN/OUT between Anthropic's content-block format (our canonical
in-memory/on-disk shape) and OpenAI's function-calling format on every
call. The stored transcript in session.json stays Anthropic-style, so
switching providers mid-session would in principle work.

Config via env (read by the openai SDK):
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_ENDPOINT
    OPENAI_API_VERSION         (e.g. "2024-10-21")
    The `model` field in agent.yaml is the Azure *deployment* name.

`openai>=1.0` is a required runtime dependency.
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


_STOP_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    # content_filter is NOT mapped to end_turn — a filtered completion is
    # not a normal completion. Leave it as-is so the agent loop can see
    # the difference if it wants to react.
}


class AzureOpenAIClient:
    def __init__(self, **sdk_kwargs: Any) -> None:
        try:
            from openai import AzureOpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "The azure_openai provider needs `openai>=1.0` installed "
                "(it is a required runtime dependency; run `uv sync`)."
            ) from e

        self._client = AzureOpenAI(**sdk_kwargs)

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


# --- Translation: Anthropic → OpenAI ---------------------------------------


def _to_openai_messages(system: str, messages: list[Message]) -> list[dict]:
    """Flatten Anthropic content-block messages into OpenAI chat shape."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    for m in messages:
        role = m["role"]
        content = m["content"]

        if role == "user":
            # A user message may contain text blocks and/or tool_result
            # blocks. OpenAI requires separate "tool" messages per result,
            # plus at most one "user" message for any remaining text.
            text_pieces: list[str] = []
            for block in content:
                bt = block.get("type")
                if bt == "text":
                    text_pieces.append(block.get("text", ""))
                elif bt == "tool_result":
                    # Preserve Anthropic's is_error signal — OpenAI's
                    # tool role has no equivalent field, so encode it
                    # inline so the LLM can see the tool failed.
                    body = _stringify_tool_result(block.get("content"))
                    if block.get("is_error"):
                        body = f"[tool error] {body}"
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": body,
                        }
                    )
                else:
                    raise ValueError(
                        f"unsupported user content block type {bt!r} for azure_openai "
                        "(provider translates only text/tool_result blocks)"
                    )
            if text_pieces:
                out.append({"role": "user", "content": "\n".join(text_pieces)})

        elif role == "assistant":
            text_pieces = []
            tool_calls: list[dict] = []
            for block in content:
                bt = block.get("type")
                if bt == "text":
                    text_pieces.append(block.get("text", ""))
                elif bt == "tool_use":
                    tool_calls.append(
                        {
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )
                else:
                    raise ValueError(
                        f"unsupported assistant content block type {bt!r} for azure_openai "
                        "(provider translates only text/tool_use blocks)"
                    )
            msg: dict = {"role": "assistant"}
            # OpenAI: content may be null when tool_calls present.
            msg["content"] = "\n".join(text_pieces) if text_pieces else None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)

    return out


def _stringify_tool_result(content: object) -> str:
    """OpenAI `tool` messages take a string content. Anthropic tool_result
    content is often already a string but can be a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block))
        return "\n".join(parts)
    return json.dumps(content)


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# --- Translation: OpenAI → Anthropic (response side) ------------------------


def _from_openai_response(resp: Any) -> Response:
    choice = resp.choices[0]
    msg = choice.message
    finish = choice.finish_reason

    raw_content: list[dict] = []
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    tool_calls_iter = msg.tool_calls or []

    if msg.content:
        raw_content.append({"type": "text", "text": msg.content})
        text_parts.append(msg.content)
    elif not tool_calls_iter:
        # Azure can return empty content with finish_reason in {content_filter,
        # length, stop} if safety filters or a truncation wiped the output.
        # Persisting {"role": "assistant", "content": []} would be invalid
        # Anthropic-shape on replay. Insert a visible placeholder so the
        # session stays well-formed and the condition is observable.
        placeholder = f"[empty response from provider; finish_reason={finish!r}]"
        raw_content.append({"type": "text", "text": placeholder})
        text_parts.append(placeholder)

    for tc in tool_calls_iter:
        try:
            parsed_input = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            # Model returned malformed JSON. Preserve the raw string in a
            # way the agent loop can surface to the LLM on next turn.
            parsed_input = {"__malformed_arguments__": tc.function.arguments}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=parsed_input))
        raw_content.append(
            {
                "type": "tool_use",
                "id": tc.id,
                "name": tc.function.name,
                "input": parsed_input,
            }
        )

    stop_reason = _STOP_MAP.get(finish, finish)

    return Response(
        text="".join(text_parts),
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        raw_content=raw_content,
    )


def _stream_translate(sdk_stream: Any) -> Iterator[ChatChunk]:
    """Translate an OpenAI streaming response into canonical ChatChunks.

    OpenAI streams text in `delta.content` and tool calls in
    `delta.tool_calls[i]`, where i is the tool-call slot and fields
    arrive incrementally (id only on the first delta for slot i;
    function.name typically complete on first delta; function.arguments
    accumulates as a JSON string). We buffer per-index and emit one
    `tool_use` ChatChunk per slot after the stream finishes.
    """
    text_parts: list[str] = []
    stop_reason: str | None = None
    # slot_index → {id, name, arguments_raw}
    tool_buffers: dict[int, dict[str, str]] = {}

    for chunk in sdk_stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue

        content = getattr(delta, "content", None)
        if content:
            text_parts.append(content)
            yield ChatChunk(kind="text_delta", text=content)

        tc_deltas = getattr(delta, "tool_calls", None) or []
        for tc_delta in tc_deltas:
            idx = getattr(tc_delta, "index", 0)
            buf = tool_buffers.setdefault(
                idx, {"id": "", "name": "", "arguments_raw": ""}
            )
            tc_id = getattr(tc_delta, "id", None)
            if tc_id:
                buf["id"] = tc_id
            fn = getattr(tc_delta, "function", None)
            if fn is not None:
                fn_name = getattr(fn, "name", None)
                if fn_name:
                    buf["name"] += fn_name
                fn_args = getattr(fn, "arguments", None)
                if fn_args:
                    buf["arguments_raw"] += fn_args

        finish = getattr(choice, "finish_reason", None)
        if finish:
            stop_reason = _STOP_MAP.get(finish, finish)

    raw_content: list[dict] = []
    text_joined = "".join(text_parts)
    if text_joined:
        raw_content.append({"type": "text", "text": text_joined})

    tool_calls: list[ToolCall] = []
    for idx in sorted(tool_buffers):
        buf = tool_buffers[idx]
        raw = buf["arguments_raw"]
        try:
            input_obj = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            input_obj = {"__malformed_arguments__": raw}
        tc = ToolCall(id=buf["id"], name=buf["name"], input=input_obj)
        tool_calls.append(tc)
        raw_content.append(
            {
                "type": "tool_use",
                "id": buf["id"],
                "name": buf["name"],
                "input": input_obj,
            }
        )
        yield ChatChunk(kind="tool_use", tool_call=tc)

    if not text_joined and not tool_calls:
        # Same safeguard as _from_openai_response — filtered/empty
        # responses must not persist invalid Anthropic-shape content.
        placeholder = (
            f"[empty response from provider; finish_reason={stop_reason!r}]"
        )
        raw_content.append({"type": "text", "text": placeholder})
        text_joined = placeholder

    final = Response(
        text=text_joined,
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
    return AzureOpenAIClient(**kwargs)


# No model_prefixes: Azure deployments use custom deployment names
# (e.g., "my-gpt4-prod") that don't match gpt-*. Set `provider: azure_openai`
# explicitly in agent.yaml to route here.
register("azure_openai", _factory)
