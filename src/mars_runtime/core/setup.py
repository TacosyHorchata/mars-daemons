"""One-call initialization for the agents_v2 module."""

from __future__ import annotations

from .config import AgentConfig, configure, reset_config
from .events import EventSink, reset_sink, set_sink
from .providers import (
    MemoryProvider,
    RulesProvider,
    SkillsProvider,
    reset_memory_provider,
    reset_rules_provider,
    reset_skills_provider,
    set_memory_provider,
    set_rules_provider,
    set_skills_provider,
)
from .store import ConversationStore, reset_store, set_store
from .tools import (
    BaseTool,
    DynamicToolProvider,
    register_tools,
    reset_registry,
    set_dynamic_tool_provider,
)


def setup_agents(
    config: AgentConfig,
    store: ConversationStore,
    sink: EventSink,
    tools: list[BaseTool],
    *,
    skills_provider: SkillsProvider | None = None,
    rules_provider: RulesProvider | None = None,
    memory_provider: MemoryProvider | None = None,
    dynamic_tool_provider: DynamicToolProvider | None = None,
    replace: bool = True,
) -> None:
    if replace:
        reset_agents()
    configure(config)
    set_store(store)
    set_sink(sink)
    register_tools(tools)
    if skills_provider is not None:
        set_skills_provider(skills_provider)
    if rules_provider is not None:
        set_rules_provider(rules_provider)
    if memory_provider is not None:
        set_memory_provider(memory_provider)
    if dynamic_tool_provider is not None:
        set_dynamic_tool_provider(dynamic_tool_provider)


async def shutdown_agents() -> None:
    import sys
    _pkg = __package__ or __name__.rsplit(".", 1)[0]
    events_mod = sys.modules.get(f"{_pkg}.events")
    if events_mod is not None:
        current_sink = getattr(events_mod, "_sink", None)
        close_fn = getattr(current_sink, "close", None)
        if callable(close_fn):
            await close_fn()
    reset_agents()


def reset_agents() -> None:
    reset_config()
    reset_registry()
    reset_store()
    reset_sink()
    reset_skills_provider()
    reset_rules_provider()
    reset_memory_provider()
