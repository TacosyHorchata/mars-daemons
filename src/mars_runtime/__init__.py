"""mars_runtime — standalone agent backend built from the agents_v2 core.

Exportable kernel: LLM calls, tool dispatch, SSE streaming.
Host-specific persistence/auth live under ``mars_runtime.host``.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise RuntimeError("mars_runtime requires Python 3.11+")

__version__ = "0.4.0"

from .core.setup import setup_agents, shutdown_agents, reset_agents  # noqa: E402
from .core.config import AgentConfig  # noqa: E402
from .core.tools import BaseTool, ToolResult, AuthContext  # noqa: E402
from .core.store import ConversationStore, ConversationContext, PersistedState, UsageMetrics  # noqa: E402
from .core.providers import (  # noqa: E402
    SkillsProvider,
    RulesProvider,
    MemoryProvider,
    SkillDefinition,
    FileRef,
)
from .core.events import EventSink, SSEEventSink  # noqa: E402
from .core.loop import run_turn  # noqa: E402

__all__ = [
    "setup_agents",
    "shutdown_agents",
    "reset_agents",
    "AgentConfig",
    "BaseTool",
    "ToolResult",
    "AuthContext",
    "ConversationStore",
    "ConversationContext",
    "PersistedState",
    "UsageMetrics",
    "SkillsProvider",
    "RulesProvider",
    "MemoryProvider",
    "SkillDefinition",
    "FileRef",
    "EventSink",
    "SSEEventSink",
    "run_turn",
]
