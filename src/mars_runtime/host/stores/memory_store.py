"""File-backed cross-conversation memory store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class FileMemoryStore:
    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / "memory.json"
        self._lock = asyncio.Lock()

    async def save_memory(self, org_id: str, agent_id: str, key: str, value: Any) -> None:
        async with self._lock:
            doc = await self._read_doc()
            doc.setdefault(org_id, {}).setdefault(agent_id, {})[key] = value
            await self._write_doc(doc)

    async def load_memories(self, org_id: str, agent_id: str) -> list[dict[str, Any]]:
        doc = await self._read_doc()
        scoped = (((doc.get(org_id) or {}).get(agent_id)) or {})
        return [{"key": key, "value": value} for key, value in scoped.items()]

    async def delete_memory(self, org_id: str, agent_id: str, key: str) -> None:
        async with self._lock:
            doc = await self._read_doc()
            scoped = (((doc.get(org_id) or {}).get(agent_id)) or {})
            scoped.pop(key, None)
            await self._write_doc(doc)

    async def _read_doc(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    async def _write_doc(self, doc: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = json.dumps(doc, ensure_ascii=True, sort_keys=True, indent=2)
        await asyncio.to_thread(tmp.write_text, payload, encoding="utf-8")
        await asyncio.to_thread(tmp.replace, self._path)
