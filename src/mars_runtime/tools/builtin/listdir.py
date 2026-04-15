"""List — directory listing with file/dir typing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import Tool, ToolOutput
from ..registry import register


def _list(input_: dict[str, Any]) -> ToolOutput:
    path = input_.get("path", ".")
    p = Path(path)
    if not p.exists():
        return ToolOutput(f"path not found: {path}", is_error=True)
    if not p.is_dir():
        return ToolOutput(f"not a directory: {path}", is_error=True)

    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    lines = [f"{'dir ' if e.is_dir() else 'file'}\t{e.name}" for e in entries]
    return ToolOutput("\n".join(lines) or "(empty directory)")


register(
    Tool(
        name="list",
        description="List the contents of a directory. Returns each entry with a file/dir marker.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path.", "default": "."},
            },
        },
        fn=_list,
    )
)
