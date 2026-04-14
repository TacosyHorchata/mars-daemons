"""Provider-neutral LLM client protocol.

The `LLMClient` Protocol is the only interface `agent.py` knows. Every
provider implementation (AnthropicClient, AzureOpenAIClient, ...) speaks
this shape. Adding a provider = one new file + `register()` call.

Message format is Anthropic's content-block shape (text + tool_use +
tool_result). This is the canonical format in memory and on disk
(session.json). Non-Anthropic providers translate IN (our → theirs) at
call time and OUT (theirs → ours) at response time, so the stored
transcript is provider-agnostic.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TypedDict, runtime_checkable


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


@dataclass
class ChatChunk:
    """One slice of a streaming LLM response.

    Canonical kinds:
      - "text_delta": incremental token(s) arriving. `text` holds the piece.
      - "tool_use": a complete tool-use block decoded from the stream.
        `tool_call` holds id/name/input.
      - "message_stop": end marker. `stop_reason` and `final_response`
        are populated so callers that don't care about streaming can
        just grab the final Response from the last chunk.

    The chat_stream() iterator always ends with exactly one message_stop.
    Providers translate their native event shapes into this canonical
    form so agent.py and client-side wrappers don't need to know which
    provider is in use.
    """

    kind: Literal["text_delta", "tool_use", "message_stop"]
    text: str = ""
    tool_call: ToolCall | None = None
    stop_reason: str | None = None
    final_response: Response | None = None


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

    def chat_stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[ChatChunk]: ...


def fallback_chat_stream(
    client: LLMClient,
    *,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    model: str,
    max_tokens: int,
) -> Iterator[ChatChunk]:
    """Default chat_stream for providers that haven't implemented streaming.

    Calls sync chat(), then yields a single message_stop chunk with the
    full response attached. Callers that iterate see the complete answer
    in one shot — no UX win, but the interface is satisfied.
    """
    resp = client.chat(
        system=system, messages=messages, tools=tools, model=model, max_tokens=max_tokens
    )
    for tc in resp.tool_calls:
        yield ChatChunk(kind="tool_use", tool_call=tc)
    if resp.text:
        yield ChatChunk(kind="text_delta", text=resp.text)
    yield ChatChunk(
        kind="message_stop",
        stop_reason=resp.stop_reason,
        final_response=resp,
    )


# --- Registry ---------------------------------------------------------------

_PROVIDERS: dict[str, Callable[..., LLMClient]] = {}
_PREFIX_INDEX: list[tuple[str, str]] = []  # [(prefix_lowercase, provider_name), ...]


class ProviderCollision(ValueError):
    pass


def register(
    name: str,
    factory: Callable[..., LLMClient],
    *,
    model_prefixes: list[str] | None = None,
) -> None:
    """Register a provider factory.

    `model_prefixes` are non-empty lowercase model-name prefixes this
    provider claims. `infer_provider()` uses them to route when `provider:`
    is not set in agent.yaml. Longest-match wins.

    Collisions raise `ProviderCollision` instead of silently overwriting:
    registering the same `name` twice with different factories, or two
    providers claiming the same prefix, is almost certainly a bug.
    """
    if not name:
        raise ValueError("provider name cannot be empty")
    if name in _PROVIDERS and _PROVIDERS[name] is not factory:
        raise ProviderCollision(
            f"provider {name!r} is already registered with a different factory"
        )

    if model_prefixes:
        for prefix in model_prefixes:
            if not prefix:
                raise ValueError(
                    f"provider {name!r}: empty model prefix is not allowed "
                    "(it would match every model)"
                )
            lowered = prefix.lower()
            for existing_prefix, existing_name in _PREFIX_INDEX:
                if existing_prefix == lowered and existing_name != name:
                    raise ProviderCollision(
                        f"model prefix {prefix!r} is already claimed by "
                        f"provider {existing_name!r}"
                    )

    _PROVIDERS[name] = factory
    if model_prefixes:
        existing_pairs = set(_PREFIX_INDEX)
        for prefix in model_prefixes:
            pair = (prefix.lower(), name)
            if pair not in existing_pairs:
                _PREFIX_INDEX.append(pair)
                existing_pairs.add(pair)
        _PREFIX_INDEX.sort(key=lambda p: len(p[0]), reverse=True)


def get(name: str, **kwargs: Any) -> LLMClient:
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown LLM provider {name!r}. registered: {sorted(_PROVIDERS)}"
        )
    return _PROVIDERS[name](**kwargs)


def registered() -> list[str]:
    return sorted(_PROVIDERS)


def infer_provider(model: str) -> str:
    """Guess the provider from the model name via registered prefixes."""
    if not isinstance(model, str) or not model:
        raise ValueError(
            f"model must be a non-empty string, got {model!r}"
        )
    m = model.lower()
    for prefix, name in _PREFIX_INDEX:
        if m.startswith(prefix):
            return name
    raise ValueError(
        f"cannot infer provider from model={model!r}; "
        f"set `provider:` in agent.yaml or register a new module with "
        f"matching model_prefixes"
    )


def load_all() -> None:
    """Import every provider module in this package so each self-registers.

    Drop-a-file extensibility: add `llm_client/newprovider.py` with a
    top-level `register(...)` call and it shows up. Files starting with
    underscore and `base.py` itself are skipped.

    Scope note: every non-underscore `.py` file in this package is
    imported at startup. Use `_helpers.py` style names (underscore prefix)
    for any non-provider helper modules so they are not auto-loaded.
    Modules that do not call `register()` are harmless no-ops.
    """
    import importlib
    import pkgutil
    from pathlib import Path

    pkg_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(pkg_dir)]):
        name = mod_info.name
        if name.startswith("_") or name == "base":
            continue
        importlib.import_module(f".{name}", package=__package__)
