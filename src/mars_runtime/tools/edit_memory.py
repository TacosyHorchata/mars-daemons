"""edit_memory — agent's writable working memory with cross-conv persistence."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..core.providers import get_memory_provider
from ..core.tools import AuthContext, BaseTool, ToolResult

logger = logging.getLogger(__name__)


def _parse_dotted_key(key: str) -> list[str]:
    if not key or not key.strip():
        raise ValueError("key must not be empty")
    parts = [seg.strip() for seg in key.strip().split(".")]
    if any(not p for p in parts):
        raise ValueError(f"key '{key}' has empty segments")
    return parts


class EditMemoryTool(BaseTool):
    name = "edit_memory"
    description = (
        "Save a durable note to your working memory (scratchpad). "
        "Use whenever the user states a preference, corrects your output, "
        "or you reach a finding worth preserving. Notes persist for the "
        "rest of the conversation (and cross-conversation if persist=true). "
        "Every note REQUIRES a 'why'."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Dotted path in the scratchpad. Examples: "
                    "'notes.user_prefs.units', 'notes.findings.weight_delta'."
                ),
            },
            "value": {
                "description": "The value to save (string, number, list, dict).",
            },
            "why": {
                "type": "string",
                "description": "One sentence justifying the note. Required.",
            },
            "persist": {
                "type": "boolean",
                "description": (
                    "Also save to cross-conversation memory (default true). "
                    "Set false for ephemeral notes."
                ),
                "default": True,
            },
        },
        "required": ["key", "value", "why"],
    }

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        raw_key = input.get("key", "")
        why = str(input.get("why", "")).strip()
        if not why:
            return ToolResult(
                success=False,
                error="'why' is required — justify in one sentence why this note is worth remembering.",
            )

        if "value" not in input:
            return ToolResult(success=False, error="'value' is required")
        value = input["value"]

        try:
            segments = _parse_dotted_key(str(raw_key))
        except ValueError as exc:
            return ToolResult(success=False, error=f"Invalid key: {exc}")

        scratchpad: dict = state.setdefault("scratchpad", {})

        # Walk/create the nested path
        cursor: Any = scratchpad
        for seg in segments[:-1]:
            existing = cursor.get(seg)
            if existing is None:
                existing = {}
                cursor[seg] = existing
            elif not isinstance(existing, dict):
                return ToolResult(
                    success=False,
                    error=(
                        f"cannot nest under '{'.'.join(segments[:segments.index(seg)+1])}' "
                        f"— existing value is {type(existing).__name__}, not a dict."
                    ),
                )
            cursor = existing

        leaf = segments[-1]
        existed = leaf in cursor
        cursor[leaf] = value

        log: list = scratchpad.setdefault("_log", [])
        log.append({
            "key": ".".join(segments),
            "why": why,
            "action": "updated" if existed else "created",
            "at": datetime.now(timezone.utc).isoformat(),
        })

        persisted_to_memory = False
        if input.get("persist", True):
            provider = get_memory_provider()
            agent_id = state.get("agent_id", "")
            if provider and agent_id:
                try:
                    await provider.save_memory(
                        org_id=auth.org_id,
                        agent_id=agent_id,
                        key=".".join(segments),
                        value=value,
                    )
                    persisted_to_memory = True
                except Exception:
                    logger.debug("agents_v2.edit_memory.persist_failed", exc_info=True)

        return ToolResult(
            success=True,
            data={
                "key": ".".join(segments),
                "action": "updated" if existed else "created",
                "saved": True,
                "persisted_to_memory": persisted_to_memory,
            },
        )
