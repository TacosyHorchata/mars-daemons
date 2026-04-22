"""Tool infrastructure: BaseTool, ToolResult, AuthContext, registry, dynamic overlay."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .exceptions import serialize_exception_details

logger = logging.getLogger(__name__)


@dataclass
class AuthContext:
    org_id: str
    user_id: str | None = None
    bearer_token: str | None = None
    request_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str | None = None
    error_details: dict[str, Any] | None = None
    tokens_used: int = 0
    next_status: str | None = None


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict
    execution_mode: str = "parallel"

    @abstractmethod
    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        ...

    async def execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        try:
            return await self._execute(input, auth, state)
        except Exception as e:
            logger.error(f"[{self.name}] Execution error: {e}", exc_info=True)
            return ToolResult(
                success=False,
                error=str(e),
                error_details=serialize_exception_details(
                    e,
                    phase="tool_execute",
                    tool=self.name,
                    input_payload=input,
                ),
            )

    def to_function_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


# ─── Global (builtin) tool registry ────────────────────────────────

_TOOL_REGISTRY: dict[str, BaseTool] = {}

# Per-conversation dynamic tool overlay (ContextVar for task isolation)
_dynamic_tool_overlay: ContextVar[dict[str, BaseTool] | None] = ContextVar(
    "agents_v2_dynamic_tool_overlay", default=None,
)

DynamicToolProvider = Callable[[str], Awaitable[list[BaseTool]]]
_dynamic_tool_provider: DynamicToolProvider | None = None

# Per-turn cleanup registry
_pending_turn_cleanups: ContextVar[list[Callable[[], Awaitable[None]]] | None] = ContextVar(
    "agents_v2_pending_turn_cleanups", default=None,
)


def register_tool(tool: BaseTool) -> None:
    _TOOL_REGISTRY[tool.name] = tool


def register_tools(tools: list[BaseTool]) -> None:
    for t in tools:
        _TOOL_REGISTRY[t.name] = t


def _visible_tools() -> dict[str, BaseTool]:
    overlay = _dynamic_tool_overlay.get()
    if not overlay:
        return dict(_TOOL_REGISTRY)
    merged = dict(overlay)
    merged.update(_TOOL_REGISTRY)
    return merged


def build_all_tool_definitions() -> list[dict]:
    return [tool.to_function_definition() for tool in _visible_tools().values()]


def get_tool_by_name(name: str) -> BaseTool | None:
    tool = _TOOL_REGISTRY.get(name)
    if tool is not None:
        return tool
    overlay = _dynamic_tool_overlay.get()
    if overlay:
        return overlay.get(name)
    return None


def get_all_tools() -> dict[str, BaseTool]:
    return _visible_tools()


def get_builtin_names() -> frozenset[str]:
    return frozenset(_TOOL_REGISTRY.keys())


def reset_registry() -> None:
    global _dynamic_tool_provider
    _TOOL_REGISTRY.clear()
    _dynamic_tool_provider = None


def set_dynamic_tool_provider(provider: DynamicToolProvider | None) -> None:
    global _dynamic_tool_provider
    _dynamic_tool_provider = provider


def get_dynamic_tool_provider() -> DynamicToolProvider | None:
    return _dynamic_tool_provider


def set_dynamic_tools_for_turn(tools: list[BaseTool]) -> Token:
    overlay: dict[str, BaseTool] = {}
    duplicates: list[str] = []
    for t in tools:
        if t.name in overlay:
            duplicates.append(t.name)
        overlay[t.name] = t

    if duplicates:
        logger.warning(
            "agents_v2.dynamic_tools.duplicate_names",
            extra={"duplicate_names": duplicates},
        )

    shadowed_builtins = [name for name in overlay if name in _TOOL_REGISTRY]
    if shadowed_builtins:
        logger.warning(
            "agents_v2.dynamic_tools.builtin_collision",
            extra={"shadowed_builtin_names": shadowed_builtins, "policy": "builtins_win"},
        )

    return _dynamic_tool_overlay.set(overlay)


def reset_dynamic_tools_for_turn(token: Token) -> None:
    _dynamic_tool_overlay.reset(token)


def init_turn_cleanups() -> Token:
    return _pending_turn_cleanups.set([])


def register_turn_cleanup(cleanup: Callable[[], Awaitable[None]]) -> None:
    pending = _pending_turn_cleanups.get()
    if pending is None:
        logger.debug("agents_v2.turn_cleanups.called_outside_turn — cleanup dropped")
        return
    pending.append(cleanup)


async def drain_turn_cleanups() -> None:
    pending = _pending_turn_cleanups.get() or []
    for cleanup in pending:
        try:
            await cleanup()
        except Exception as exc:
            logger.warning(
                "agents_v2.turn_cleanups.failure",
                extra={"error": str(exc)},
                exc_info=True,
            )


def reset_turn_cleanups(token: Token) -> None:
    _pending_turn_cleanups.reset(token)
