"""Tool registry — global map of name → Tool, plus ToolRegistry scoped
to a single agent's allowlist.

`load_all()` imports every module under `tools/builtin/` so the
built-in tools self-register. Third-party tools can register by
importing `from mars_runtime.tools.registry import register` and
calling it at module load time.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolOutput


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
    """Import every tool module under tools/builtin/ so each self-registers."""
    from .builtin import bash, edit, glob, grep, listdir, read, websearch  # noqa: F401
