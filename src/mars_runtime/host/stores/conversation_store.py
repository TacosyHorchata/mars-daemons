"""Filesystem-backed conversation store for the standalone host."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.config import get_config
from ...core.exceptions import OrgScopingError
from ...core.store import ConversationContext, PersistedState, UsageMetrics

CONVERSATIONS_PER_PAGE = 20


class LocalConversationStore:
    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir) / "conversations"
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, conversation_id: str, *, org_id: str) -> PersistedState | None:
        doc = await self._read_matching_doc(conversation_id, org_id=org_id)
        if not doc or not doc.get("context"):
            return None
        return _deserialize_persisted_state(doc)

    async def save(self, conversation_id: str, state: PersistedState, *, org_id: str) -> None:
        async with self._lock_for(conversation_id):
            doc = await self._read_doc(conversation_id)
            if not doc or str(doc.get("org_id", "")) != org_id:
                raise OrgScopingError(f"Save did not match any conversation for org {org_id}")

            compacted = _compact_state(state)
            doc["context"] = _serialize_context_doc(compacted.context)
            doc["system_prompt"] = compacted.context.system_prompt
            doc["status"] = compacted.status
            doc["usage"] = _serialize_usage_doc(compacted.usage)
            doc["last_message_at"] = compacted.last_message_at
            await self._write_doc(conversation_id, doc)

    async def update_status(self, conversation_id: str, status: str, *, org_id: str) -> None:
        async with self._lock_for(conversation_id):
            doc = await self._read_doc(conversation_id)
            if not doc or str(doc.get("org_id", "")) != org_id:
                return
            doc["status"] = status
            if status == "working":
                doc["last_message_at"] = _utcnow()
            await self._write_doc(conversation_id, doc)

    async def update_title(self, conversation_id: str, title: str, *, org_id: str) -> None:
        async with self._lock_for(conversation_id):
            doc = await self._read_doc(conversation_id)
            if not doc or str(doc.get("org_id", "")) != org_id or doc.get("title"):
                return
            doc["title"] = title
            await self._write_doc(conversation_id, doc)

    async def create(self, org_id: str, created_by: str, *, agent_id: str = "default") -> str:
        conversation_id = f"conv_{uuid.uuid4().hex}"
        now = _utcnow()
        doc = {
            "id": conversation_id,
            "agent_id": agent_id,
            "org_id": org_id,
            "created_by": created_by,
            "status": "working",
            "title": None,
            "context": None,
            "usage": _serialize_usage_doc(UsageMetrics()),
            "last_message_at": now,
            "created_at": now,
        }
        await self._write_doc(conversation_id, doc)
        return conversation_id

    async def list_conversations(
        self,
        org_id: str,
        page: int,
        *,
        created_by: str | None = None,
    ) -> list[dict[str, Any]]:
        docs = await self._read_all_docs()
        filtered = [
            doc for doc in docs
            if str(doc.get("org_id", "")) == org_id
            and (created_by is None or str(doc.get("created_by", "")) == created_by)
        ]
        filtered.sort(key=lambda doc: str(doc.get("last_message_at") or ""), reverse=True)
        page = max(page, 1)
        start = (page - 1) * CONVERSATIONS_PER_PAGE
        end = start + CONVERSATIONS_PER_PAGE
        return [_serialize_conversation_summary(doc) for doc in filtered[start:end]]

    async def get(
        self,
        conversation_id: str,
        *,
        org_id: str,
        created_by: str | None = None,
    ) -> dict[str, Any] | None:
        doc = await self._read_matching_doc(conversation_id, org_id=org_id, created_by=created_by)
        if not doc:
            return None
        return _serialize_conversation(doc)

    async def claim_turn(
        self,
        conversation_id: str,
        *,
        org_id: str,
        expected_last_message_at: str,
        allowed_statuses: tuple[str, ...],
    ) -> bool:
        if not allowed_statuses:
            return False

        async with self._lock_for(conversation_id):
            doc = await self._read_doc(conversation_id)
            if not doc or str(doc.get("org_id", "")) != org_id:
                return False
            if str(doc.get("status", "")) not in allowed_statuses:
                return False
            if str(doc.get("last_message_at") or "") != expected_last_message_at:
                return False
            doc["status"] = "working"
            doc["last_message_at"] = _utcnow()
            await self._write_doc(conversation_id, doc)
            return True

    async def list_durable_events(
        self,
        conversation_id: str,
        *,
        after_sequence: int,
        org_id: str,
    ) -> list[dict[str, Any]]:
        doc = await self._read_matching_doc(conversation_id, org_id=org_id)
        if not doc:
            return []
        events = ((doc.get("context") or {}).get("_durable_events") or [])
        return [event for event in events if int(event.get("sequence", 0) or 0) > after_sequence]

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def _path_for(self, conversation_id: str) -> Path:
        return self._root / f"{conversation_id}.json"

    async def _read_matching_doc(
        self,
        conversation_id: str,
        *,
        org_id: str,
        created_by: str | None = None,
    ) -> dict[str, Any] | None:
        doc = await self._read_doc(conversation_id)
        if not doc or str(doc.get("org_id", "")) != org_id:
            return None
        if created_by is not None and str(doc.get("created_by", "")) != created_by:
            return None
        return doc

    async def _read_doc(self, conversation_id: str) -> dict[str, Any] | None:
        path = self._path_for(conversation_id)
        if not path.exists():
            return None
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return json.loads(text)

    async def _write_doc(self, conversation_id: str, doc: dict[str, Any]) -> None:
        path = self._path_for(conversation_id)
        payload = json.dumps(doc, ensure_ascii=True, sort_keys=True, indent=2)
        tmp = path.with_suffix(".tmp")
        await asyncio.to_thread(tmp.write_text, payload, encoding="utf-8")
        await asyncio.to_thread(tmp.replace, path)

    async def _read_all_docs(self) -> list[dict[str, Any]]:
        paths = sorted(self._root.glob("*.json"))
        docs: list[dict[str, Any]] = []
        for path in paths:
            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                docs.append(json.loads(text))
            except (OSError, json.JSONDecodeError):
                continue
        return docs


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_usage_doc(usage: UsageMetrics) -> dict[str, int]:
    return {
        "llm_calls": usage.llm_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "tool_calls": usage.tool_calls,
    }


def _serialize_context_doc(context: ConversationContext) -> dict[str, Any]:
    return {
        "messages": context.messages,
        "tool_calls": context.tool_calls,
        "conversation": context.conversation,
        "scratchpad": context.scratchpad,
        "files": context.files,
        "system_prompt": context.system_prompt,
        "active_skills": context.active_skills,
        "_event_sequence": context._event_sequence,
        "_durable_events": context._durable_events,
    }


def _deserialize_context(doc: dict[str, Any]) -> ConversationContext:
    ctx = doc.get("context") or {}
    return ConversationContext(
        messages=list(ctx.get("messages") or []),
        tool_calls=list(ctx.get("tool_calls") or []),
        conversation=list(ctx.get("conversation") or []),
        scratchpad=dict(ctx.get("scratchpad") or {}),
        files=list(ctx.get("files") or []),
        system_prompt=ctx.get("system_prompt") or doc.get("system_prompt"),
        active_skills=list(ctx.get("active_skills") or []),
        _event_sequence=int(ctx.get("_event_sequence", 0) or 0),
        _durable_events=list(ctx.get("_durable_events") or []),
    )


def _deserialize_persisted_state(doc: dict[str, Any]) -> PersistedState:
    usage = doc.get("usage") or {}
    return PersistedState(
        context=_deserialize_context(doc),
        status=str(doc.get("status", "idle")),
        usage=UsageMetrics(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            tool_calls=int(usage.get("tool_calls", 0) or 0),
        ),
        last_message_at=str(doc.get("last_message_at") or _utcnow()),
    )


def _serialize_conversation_summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc.get("id", "")),
        "agent_id": str(doc.get("agent_id", "")),
        "org_id": str(doc.get("org_id", "")),
        "created_by": str(doc.get("created_by", "")),
        "status": str(doc.get("status", "idle")),
        "title": doc.get("title"),
        "last_message_at": str(doc.get("last_message_at") or ""),
        "created_at": str(doc.get("created_at") or ""),
    }


def _serialize_conversation(doc: dict[str, Any]) -> dict[str, Any]:
    summary = _serialize_conversation_summary(doc)
    context = _deserialize_context(doc)
    usage = doc.get("usage") or {}
    summary.update(
        {
            "context": _serialize_context_doc(context),
            "usage": {
                "llm_calls": int(usage.get("llm_calls", 0) or 0),
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "tool_calls": int(usage.get("tool_calls", 0) or 0),
            },
            "system_prompt": context.system_prompt,
        },
    )
    return summary


def _compact_state(state: PersistedState) -> PersistedState:
    config = get_config()
    context = state.context
    messages = context.messages
    if config.max_persisted_messages > 0 and len(messages) > config.max_persisted_messages:
        messages = messages[-config.max_persisted_messages :]

    compacted_context = ConversationContext(
        messages=messages,
        tool_calls=context.tool_calls,
        conversation=context.conversation,
        scratchpad=context.scratchpad,
        files=context.files,
        system_prompt=context.system_prompt,
        active_skills=context.active_skills,
        _event_sequence=context._event_sequence,
        _durable_events=context._durable_events,
    )
    return PersistedState(
        context=compacted_context,
        status=state.status,
        usage=state.usage,
        last_message_at=state.last_message_at,
    )
