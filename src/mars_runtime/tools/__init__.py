"""Tools subpackage.

Public surface:
  - Types:     `Tool`, `ToolOutput`     (from .base)
  - Registry:  `register`, `ToolRegistry`, `ALL_TOOLS`, `load_all`
                                         (from .registry)

Built-in tools live in `tools/builtin/` and self-register when imported
via `registry.load_all()`.

Third-party tool authors: import what you need with explicit paths to
avoid triggering `load_all()`:

    from mars_runtime.tools.base import Tool, ToolOutput
    from mars_runtime.tools.registry import register
"""

from __future__ import annotations

from .base import Tool, ToolOutput
from .registry import ALL_TOOLS, ToolRegistry, load_all, register

__all__ = [
    "ALL_TOOLS",
    "Tool",
    "ToolOutput",
    "ToolRegistry",
    "load_all",
    "register",
]
