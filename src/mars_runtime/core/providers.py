"""Provider protocols: Skills, Rules, Memory. Host implements, core consumes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SkillDefinition:
    name: str
    description: str
    prompt_template: str
    input_schema: dict = field(default_factory=dict)
    required_tools: list[str] = field(default_factory=list)
    activation_mode: str = "one_turn"
    is_shared: bool = False
    source: str = ""


@dataclass(frozen=True)
class FileRef:
    key: str
    filename: str
    mimetype: str
    size: int
    url: str = ""


@runtime_checkable
class SkillsProvider(Protocol):
    async def list_skills(self, org_id: str) -> list[SkillDefinition]: ...
    async def get_skill(self, name: str, org_id: str) -> SkillDefinition | None: ...


@runtime_checkable
class RulesProvider(Protocol):
    async def list_rules(self, org_id: str, agent_id: str | None) -> list[dict[str, Any]]: ...


@runtime_checkable
class MemoryProvider(Protocol):
    async def save_memory(self, org_id: str, agent_id: str, key: str, value: Any) -> None: ...
    async def load_memories(self, org_id: str, agent_id: str) -> list[dict[str, Any]]: ...
    async def delete_memory(self, org_id: str, agent_id: str, key: str) -> None: ...


# ─── Singletons ────────────────────────────────────────────────

_skills_provider: SkillsProvider | None = None
_rules_provider: RulesProvider | None = None
_memory_provider: MemoryProvider | None = None


def get_skills_provider() -> SkillsProvider | None:
    return _skills_provider


def set_skills_provider(provider: SkillsProvider | None) -> None:
    global _skills_provider
    _skills_provider = provider


def reset_skills_provider() -> None:
    global _skills_provider
    _skills_provider = None


def get_rules_provider() -> RulesProvider | None:
    return _rules_provider


def set_rules_provider(provider: RulesProvider | None) -> None:
    global _rules_provider
    _rules_provider = provider


def reset_rules_provider() -> None:
    global _rules_provider
    _rules_provider = None


def get_memory_provider() -> MemoryProvider | None:
    return _memory_provider


def set_memory_provider(provider: MemoryProvider | None) -> None:
    global _memory_provider
    _memory_provider = provider


def reset_memory_provider() -> None:
    global _memory_provider
    _memory_provider = None
