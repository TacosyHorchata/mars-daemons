"""ConversationStore protocol and data types. Protocol only — host provides implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class UsageMetrics:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0


@dataclass
class ConversationContext:
    messages: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    conversation: list[dict] = field(default_factory=list)
    scratchpad: dict = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str | None = None
    active_skills: list[dict] = field(default_factory=list)
    _event_sequence: int = 0
    _durable_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PersistedState:
    context: ConversationContext
    status: str
    usage: UsageMetrics
    last_message_at: str


@runtime_checkable
class ConversationStore(Protocol):
    """Core protocol — 4 methods. Host extends with create/list/get/claim_turn/list_durable_events."""

    async def load(self, conversation_id: str, *, org_id: str) -> PersistedState | None: ...
    async def save(self, conversation_id: str, state: PersistedState, *, org_id: str) -> None: ...
    async def update_status(self, conversation_id: str, status: str, *, org_id: str) -> None: ...
    async def update_title(self, conversation_id: str, title: str, *, org_id: str) -> None: ...


_store: ConversationStore | None = None


def get_store() -> ConversationStore:
    if _store is None:
        raise RuntimeError("ConversationStore not configured. Call setup_agents() first.")
    return _store


def set_store(store: ConversationStore) -> None:
    global _store
    _store = store


def reset_store() -> None:
    global _store
    _store = None
