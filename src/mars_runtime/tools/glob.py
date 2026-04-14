"""Glob — file path pattern matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Tool, ToolOutput, register


def _glob(input_: dict[str, Any]) -> ToolOutput:
    pattern = input_["pattern"]
    path = input_.get("path", ".")
    root = Path(path)
    if not root.is_dir():
        return ToolOutput(f"not a directory: {path}", is_error=True)

    matches = sorted(str(p) for p in root.glob(pattern))
    if len(matches) > 500:
        matches = matches[:500] + ["(truncated at 500 matches)"]
    return ToolOutput("\n".join(matches) or "(no matches)")


register(
    Tool(
        name="glob",
        description="Find files matching a glob pattern (e.g. '**/*.py').",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {"type": "string", "description": "Root directory.", "default": "."},
            },
            "required": ["pattern"],
        },
        fn=_glob,
    )
)
