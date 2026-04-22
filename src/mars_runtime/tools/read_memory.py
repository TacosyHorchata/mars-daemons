"""read_memory — simplified scratchpad reader. No wildcards, no array indexing."""

from __future__ import annotations

import json
from typing import Any

from ..core.tools import AuthContext, BaseTool, ToolResult
from ..core.state import _shape_map

DEFAULT_MAX_RESPONSE_BYTES = 20_000


def _parse_dotted_path(path: str) -> list[str]:
    if not path or not path.strip():
        return []
    parts = [seg.strip() for seg in path.strip().split(".")]
    if any(not p for p in parts):
        raise ValueError(f"path '{path}' has empty segments")
    return parts


def _walk_dotted(obj: Any, segments: list[str]) -> Any:
    for seg in segments:
        if not isinstance(obj, dict):
            raise KeyError(f"key '{seg}' requires a dict, got {type(obj).__name__}")
        if seg not in obj:
            available = list(obj.keys())
            raise KeyError(f"key '{seg}' not found. Available: {available}")
        obj = obj[seg]
    return obj


def _size_of(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(value))


class ReadMemoryTool(BaseTool):
    name = "read_memory"
    description = (
        "Read from your scratchpad. Empty key returns a shape map of the whole "
        "scratchpad. A dotted key drills into nested dicts (e.g., 'notes.user_prefs'). "
        "This tool is free and fast — use it whenever you need stored data."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Dotted path into the scratchpad. Empty string returns a shape map. "
                    "Examples: 'notes.user_prefs', 'extractions.inv_0'."
                ),
                "default": "",
            },
        },
    }

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        key = str(input.get("key", "")).strip()
        scratchpad: dict = state.get("scratchpad") or {}

        if not key:
            return ToolResult(
                success=True,
                data={
                    "key": "",
                    "mode": "shape_map",
                    "value": _shape_map(scratchpad, max_depth=2),
                },
            )

        try:
            segments = _parse_dotted_path(key)
        except ValueError as exc:
            return ToolResult(success=False, error=f"Invalid key: {exc}")

        try:
            value = _walk_dotted(scratchpad, segments)
        except KeyError as exc:
            return ToolResult(success=False, error=str(exc))

        size = _size_of(value)
        if size > DEFAULT_MAX_RESPONSE_BYTES:
            return ToolResult(
                success=True,
                data={
                    "key": key,
                    "mode": "too_large",
                    "size_bytes": size,
                    "max_bytes": DEFAULT_MAX_RESPONSE_BYTES,
                    "shape": _shape_map(value, max_depth=2),
                    "hint": (
                        f"Value at '{key}' is too large ({size} bytes > {DEFAULT_MAX_RESPONSE_BYTES}). "
                        "Navigate into a more specific key."
                    ),
                },
            )

        return ToolResult(
            success=True,
            data={
                "key": key,
                "mode": "value",
                "size_bytes": size,
                "value": value,
            },
        )
