"""File-backed rules provider."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class FileRulesStore:
    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / "rules.json"

    async def list_rules(self, org_id: str, agent_id: str | None) -> list[dict[str, Any]]:
        docs = await self._read_doc()
        results: list[dict[str, Any]] = []
        for doc in docs:
            if not doc.get("is_active", True):
                continue
            if doc.get("org_id") not in (None, "", org_id):
                continue
            if agent_id and doc.get("agent_id") not in (None, "", agent_id):
                continue
            results.append(
                {
                    "name": doc.get("name", ""),
                    "content": doc.get("content", ""),
                    "priority": int(doc.get("priority", 0) or 0),
                },
            )
        results.sort(key=lambda item: item.get("priority", 0), reverse=True)
        return results

    async def _read_doc(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []
