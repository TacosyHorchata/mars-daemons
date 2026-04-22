"""File-backed skills provider."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ...core.providers import SkillDefinition


class FileSkillsStore:
    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / "skills.json"

    async def list_skills(self, org_id: str) -> list[SkillDefinition]:
        docs = await self._read_doc()
        return [skill for skill in (_doc_to_skill(doc, org_id) for doc in docs) if skill is not None]

    async def get_skill(self, name: str, org_id: str) -> SkillDefinition | None:
        docs = await self._read_doc()
        for doc in docs:
            skill = _doc_to_skill(doc, org_id)
            if skill is not None and skill.name == name:
                return skill
        return None

    async def _read_doc(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []


def _doc_to_skill(doc: dict[str, Any], org_id: str) -> SkillDefinition | None:
    if not doc.get("is_active", True):
        return None
    if doc.get("org_id") not in (None, "", org_id) and not doc.get("is_shared", False):
        return None
    name = str(doc.get("name", "")).strip()
    prompt_template = str(doc.get("prompt_template", "")).strip()
    if not name or not prompt_template:
        return None
    activation_mode = str(doc.get("activation_mode", "one_turn"))
    if activation_mode not in {"one_turn", "persistent"}:
        return None
    return SkillDefinition(
        name=name,
        description=str(doc.get("description", "")).strip(),
        prompt_template=prompt_template,
        input_schema=dict(doc.get("input_schema") or {}),
        required_tools=list(doc.get("required_tools") or []),
        activation_mode=activation_mode,
        is_shared=bool(doc.get("is_shared", False)),
        source="file",
    )
