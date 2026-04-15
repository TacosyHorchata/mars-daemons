"""Read — read a file with optional line-range slicing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import Tool, ToolOutput
from ..registry import register


def _read(input_: dict[str, Any]) -> ToolOutput:
    file_path = input_["file_path"]
    offset = input_.get("offset", 0)
    limit = input_.get("limit")

    p = Path(file_path)
    if not p.exists():
        return ToolOutput(f"file not found: {file_path}", is_error=True)
    if not p.is_file():
        return ToolOutput(f"not a file: {file_path}", is_error=True)

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    end = offset + limit if limit else len(lines)
    selected = lines[offset:end]
    numbered = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(selected))
    return ToolOutput(numbered or "(empty file)")


register(
    Tool(
        name="read",
        description="Read a text file. Returns line-numbered content. Supports offset (0-based) and limit for long files.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file."},
                "offset": {"type": "integer", "description": "Line number to start from (0-based).", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to return."},
            },
            "required": ["file_path"],
        },
        fn=_read,
    )
)
