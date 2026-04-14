"""Edit — exact string replace.

Protected files (CLAUDE.md, AGENTS.md, agent.yaml) are blocked by
basename match. An agent can still bypass via: symlinks, relative paths,
or the bash tool (sed, cat >, python -c). This is consistent with the
bash tool's security model — speed bump, not sandbox. The daemon's
system prompt contract is enforced by convention and by deployment
controls (read-only FS mounts), not by this denylist alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Tool, ToolOutput, register

PROTECTED_BASENAMES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md", "agent.yaml")


def _edit(input_: dict[str, Any]) -> ToolOutput:
    file_path = input_["file_path"]
    old = input_["old_string"]
    new = input_["new_string"]

    p = Path(file_path)
    if p.name in PROTECTED_BASENAMES:
        return ToolOutput(
            f"'{p.name}' is admin-only and cannot be edited by a daemon.",
            is_error=True,
        )
    if not p.exists():
        return ToolOutput(f"file not found: {file_path}", is_error=True)

    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return ToolOutput(f"old_string not found in {file_path}", is_error=True)
    if count > 1:
        return ToolOutput(
            f"old_string appears {count} times in {file_path}; make it unique",
            is_error=True,
        )

    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return ToolOutput(f"replaced 1 occurrence in {file_path}")


register(
    Tool(
        name="edit",
        description="Replace exactly one occurrence of old_string with new_string in a file. Fails if not unique.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string", "description": "Exact string to replace. Must appear exactly once."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        fn=_edit,
    )
)
