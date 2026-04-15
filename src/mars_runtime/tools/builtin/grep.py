"""Grep — ripgrep wrapper with pure-Python fallback."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..base import Tool, ToolOutput
from ..registry import register


def _grep_rg(pattern: str, path: str, glob: str | None) -> ToolOutput:
    cmd = ["rg", "--line-number", "--no-heading", "--color=never", pattern, path]
    if glob:
        cmd[1:1] = ["--glob", glob]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode not in (0, 1):
        return ToolOutput(result.stderr or "rg failed", is_error=True)
    return ToolOutput(result.stdout.rstrip() or "(no matches)")


def _grep_python(pattern: str, path: str, glob: str | None) -> ToolOutput:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return ToolOutput(f"invalid regex: {e}", is_error=True)

    root = Path(path)
    files = root.rglob(glob) if glob else (root.rglob("*") if root.is_dir() else [root])
    matches: list[str] = []
    for f in files:
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f}:{i}:{line}")
                    if len(matches) >= 500:
                        matches.append("(truncated at 500 matches)")
                        return ToolOutput("\n".join(matches))
        except OSError:
            continue
    return ToolOutput("\n".join(matches) or "(no matches)")


def _grep(input_: dict[str, Any]) -> ToolOutput:
    pattern = input_["pattern"]
    path = input_.get("path", ".")
    glob = input_.get("glob")
    if shutil.which("rg"):
        return _grep_rg(pattern, path, glob)
    return _grep_python(pattern, path, glob)


register(
    Tool(
        name="grep",
        description="Search file contents for a regex. Uses ripgrep if available, falls back to Python re.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern."},
                "path": {"type": "string", "description": "Directory or file to search.", "default": "."},
                "glob": {"type": "string", "description": "Filter files by glob (e.g. '*.py')."},
            },
            "required": ["pattern"],
        },
        fn=_grep,
    )
)
