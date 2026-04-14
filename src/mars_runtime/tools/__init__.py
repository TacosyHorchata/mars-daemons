"""Tool registry. One Tool = (name, description, JSON Schema, fn).

Tools self-register on import. ToolRegistry applies the per-agent
allowlist from `agent.yaml: tools:` (empty list = all registered tools).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[dict[str, Any]], ToolOutput]


ALL_TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    ALL_TOOLS[tool.name] = tool


class ToolRegistry:
    def __init__(self, allowlist: list[str] | None = None):
        if allowlist:
            missing = [n for n in allowlist if n not in ALL_TOOLS]
            if missing:
                raise ValueError(f"unknown tools in allowlist: {missing}")
            self._tools = {n: ALL_TOOLS[n] for n in allowlist}
        else:
            self._tools = dict(ALL_TOOLS)

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools)

    def execute(self, name: str, input_: dict[str, Any]) -> ToolOutput:
        if name not in self._tools:
            return ToolOutput(f"tool {name!r} not available", is_error=True)
        try:
            return self._tools[name].fn(input_)
        except Exception as e:  # noqa: BLE001
            return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)


def load_all() -> None:
    """Import every tool module so each can self-register."""
    from . import bash, edit, glob, grep, listdir, read, websearch  # noqa: F401
