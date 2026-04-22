"""State shape management — ONE job: init, restore, mutate the state dict."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .store import ConversationContext, UsageMetrics


def init_state(system_prompt: str, org_id: str) -> dict:
    return {
        "messages": [{"role": "system", "content": system_prompt}],
        "tool_calls": [],
        "conversation": [],
        "scratchpad": {},
        "files": [],
        "system_prompt": system_prompt,
        "status": "idle",
        "org_id": org_id,
        "usage": UsageMetrics(),
        "active_skills": [],
        "conversation_id": None,
        "_event_sequence": 0,
        "_durable_events": [],
    }


def restore_state(context: ConversationContext, system_prompt: str, org_id: str) -> dict:
    state = init_state(system_prompt, org_id)
    state["messages"] = context.messages if context.messages else state["messages"]
    state["tool_calls"] = context.tool_calls
    state["conversation"] = context.conversation
    state["scratchpad"] = context.scratchpad
    state["files"] = context.files
    state["system_prompt"] = context.system_prompt or system_prompt
    state["active_skills"] = context.active_skills or []
    state["_event_sequence"] = context._event_sequence
    state["_durable_events"] = context._durable_events or []
    if state["messages"] and state["messages"][0]["role"] == "system":
        state["messages"][0]["content"] = system_prompt
    return state


def inject_user_message(state: dict, user_message: str, files: list[dict] | None = None) -> None:
    files = files or []
    if files:
        already_seeded = len(state["files"]) >= len(files) and state["files"][-len(files):] == files
        if already_seeded:
            base_index = len(state["files"]) - len(files)
        else:
            base_index = len(state["files"])
            for f in files:
                state["files"].append(f)

        file_refs = "\n".join(
            f"[artifact {base_index + i}] {f.get('filename', 'file')} ({f.get('mimetype', 'unknown')}, {f.get('size', 0)} bytes)"
            for i, f in enumerate(files)
        )
        content = f"{user_message}\n\nAttached files:\n{file_refs}"
    else:
        content = user_message

    append_llm_message(state, {"role": "user", "content": content})

    conversation = state.get("conversation", [])
    already_seeded = (
        len(conversation) > 0
        and conversation[-1].get("role") == "user"
        and conversation[-1].get("content") == user_message
    )
    if not already_seeded:
        save_to_conversation(
            state, "user", user_message, "user_message",
            files=[{"filename": f.get("filename", ""), "mimetype": f.get("mimetype", "")} for f in files] if files else None,
        )


def save_to_conversation(state: dict, role: str, content: str, msg_type: str, **kwargs) -> None:
    entry = {
        "role": role,
        "content": content,
        "type": msg_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for k, v in kwargs.items():
        if v is not None:
            entry[k] = v
    state["conversation"].append(entry)


def append_llm_message(state: dict, message: dict[str, Any]) -> None:
    state.setdefault("messages", []).append(message)


def inject_scratchpad(state: dict) -> None:
    if not state.get("scratchpad"):
        return
    llm_view = _scratchpad_for_llm(state["scratchpad"])
    scratchpad_content = f"[SCRATCHPAD — Persistent Notes]\n{json.dumps(llm_view, default=str, ensure_ascii=False)}\n[END SCRATCHPAD]"
    for i, msg in enumerate(state["messages"]):
        if msg.get("role") == "system" and "[SCRATCHPAD" in msg.get("content", ""):
            state["messages"][i]["content"] = scratchpad_content
            return
    insert_idx = 1 if state["messages"] and state["messages"][0].get("role") == "system" else 0
    state["messages"].insert(insert_idx, {"role": "system", "content": scratchpad_content})


def _scratchpad_for_llm(scratchpad: dict) -> dict:
    return _shape_map(scratchpad, max_depth=2)


def _shape_hint(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 80:
            return f"<str: {len(value)} chars>"
        return value
    if isinstance(value, dict):
        return f"<dict: {len(value)} keys>"
    if isinstance(value, list):
        return f"<list: {len(value)} items>"
    return f"<{type(value).__name__}>"


def _shape_map(value: Any, depth: int = 0, max_depth: int = 1) -> Any:
    if depth >= max_depth:
        return _shape_hint(value)
    if isinstance(value, dict):
        return {k: _shape_map(v, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return []
        return f"<list: {len(value)} items>"
    return _shape_hint(value)


# ─── Tool call tracking ───────────────────────────────────────────

def upsert_tool_call(
    state: dict,
    *,
    call_id: str,
    defaults: dict[str, Any] | None = None,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_calls = state.setdefault("tool_calls", [])
    for entry in tool_calls:
        if str(entry.get("call_id") or "") == call_id:
            if updates:
                entry.update({key: value for key, value in updates.items() if value is not None})
            return entry

    entry = {"call_id": call_id}
    if defaults:
        entry.update({key: value for key, value in defaults.items() if value is not None})
    if updates:
        entry.update({key: value for key, value in updates.items() if value is not None})
    tool_calls.append(entry)
    return entry


def mark_tool_call_started(
    state: dict,
    action: dict[str, Any],
    *,
    execution_mode: str,
    started_at: str | None = None,
) -> dict[str, Any]:
    timestamp = started_at or datetime.now(timezone.utc).isoformat()
    return upsert_tool_call(
        state,
        call_id=action["call_id"],
        defaults={
            "tool": action["tool"],
            "label": action.get("label", action["tool"]),
            "input": deepcopy(action.get("input")),
        },
        updates={
            "status": "started",
            "started_at": timestamp,
            "execution_mode": execution_mode,
        },
    )


def mark_tool_call_finished(
    state: dict,
    action: dict[str, Any],
    *,
    success: bool,
    output: Any = None,
    error: Any = None,
    error_details: dict[str, Any] | None = None,
    tokens: int | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    timestamp = completed_at or datetime.now(timezone.utc).isoformat()
    existing = upsert_tool_call(
        state,
        call_id=action["call_id"],
        defaults={
            "tool": action["tool"],
            "label": action.get("label", action["tool"]),
            "input": deepcopy(action.get("input")),
        },
    )
    started_at_val = existing.get("started_at")
    duration_ms = None
    if started_at_val:
        try:
            duration_ms = int(
                (
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(started_at_val).replace("Z", "+00:00"))
                ).total_seconds() * 1000,
            )
        except ValueError:
            duration_ms = None

    return upsert_tool_call(
        state,
        call_id=action["call_id"],
        updates={
            "status": "completed" if success else "error",
            "success": success,
            "output": output if success else None,
            "error": error if not success else None,
            "error_details": error_details if not success else None,
            "tokens": tokens,
            "completed_at": timestamp,
            "ended_at": timestamp,
            "duration_ms": duration_ms,
        },
    )
