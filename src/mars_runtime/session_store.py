"""JSON-per-session persistence.

One file per session at <sessions_dir>/<id>.json. Self-contained snapshot:
id, agent_name, the full agent_config used to start the session, and the
Anthropic messages[] array as of the last closed turn.

Writes are atomic via tmp-file + rename (POSIX guarantee). Reads are plain.
Not thread-safe, not multi-writer-safe — single-writer v0.2.0.

This module is supervisor-only by convention. The LLM never sees these files
as tools; the agent loop calls save() directly at end-of-turn.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

# 24 hex chars = 96 bits of entropy. Birthday collision for 1B sessions is
# still well below 10^-9, so the ID-space is safe without a uniqueness check.
_SESSION_ID_RE = re.compile(r"^sess_[0-9a-f]{24}$")


class InvalidSessionId(ValueError):
    pass


def new_id() -> str:
    return f"sess_{uuid.uuid4().hex[:24]}"


def _validate_id(session_id: str) -> None:
    """Reject anything that could escape sessions_dir via path traversal."""
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise InvalidSessionId(
            f"invalid session_id {session_id!r} "
            "(expected sess_ + 24 lowercase hex chars)"
        )


def _path_for(sessions_dir: Path, session_id: str) -> Path:
    _validate_id(session_id)
    return sessions_dir / f"{session_id}.json"


def save(
    sessions_dir: Path,
    session_id: str,
    agent_name: str,
    agent_config: dict[str, Any],
    messages: list[dict],
    *,
    created_at: int | None = None,
) -> None:
    """Atomically write the session snapshot.

    Preserves `created_at` across saves by reading the existing file if
    present. Callers may pass an explicit value (used on first save).
    """
    sessions_dir.mkdir(parents=True, exist_ok=True)
    final = _path_for(sessions_dir, session_id)

    if created_at is None:
        if final.exists():
            # Preserve the original created_at. If the existing file is
            # corrupt, surface it — silently clobbering hides bugs.
            existing = json.loads(final.read_text(encoding="utf-8"))
            created_at = int(existing["created_at"])
        else:
            created_at = int(time.time())

    payload = {
        "id": session_id,
        "agent_name": agent_name,
        "agent_config": agent_config,
        "created_at": created_at,
        "messages": messages,
    }

    # Unique tmp filename per-call avoids clobbering if two writers ever
    # collide on the same session (single-writer is the v0.2 contract, but
    # cheap insurance costs nothing).
    tmp = final.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(final)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load(sessions_dir: Path, session_id: str) -> dict[str, Any]:
    return json.loads(_path_for(sessions_dir, session_id).read_text(encoding="utf-8"))


def _count_user_turns(messages: object) -> int:
    """Count user turns (text inputs), ignoring tool_result batches.

    Defensive against malformed data (hand-edited files, planted payloads):
    non-list messages, non-dict entries, non-list content, or None blocks
    all count as zero rather than crashing.
    """
    if not isinstance(messages, list):
        return 0
    n = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                n += 1
                break
    return n


def list_recent(sessions_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    """Return session metadata, newest-first by file mtime.

    Excludes `messages` to keep the listing cheap and printable.
    """
    if not sessions_dir.exists():
        return []

    # Only accept filenames shaped like our session IDs. This keeps stray
    # tmp files, backups, or attacker-planted names out of the listing.
    files = sorted(
        (p for p in sessions_dir.glob("sess_*.json") if _SESSION_ID_RE.fullmatch(p.stem)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    out: list[dict[str, Any]] = []
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        # Trust the filename (which we validated), not the in-file `id` —
        # a planted file could claim any id. Skip if they disagree.
        if data.get("id") != p.stem:
            continue
        out.append(
            {
                "id": p.stem,
                "agent_name": data.get("agent_name"),
                "created_at": data.get("created_at"),
                "updated_at": int(p.stat().st_mtime),
                "turn_count": _count_user_turns(data.get("messages")),
            }
        )
    return out


def _is_valid_messages_shape(messages: object) -> bool:
    """Shape check matching what run() assumes downstream.

    Each message must be {role: user|assistant, content: list[dict]}. The
    list items must be dicts (Anthropic content blocks are always dicts
    with a `type` field); anything else crashes the agent loop when it
    calls `block.get(...)` during duplicate-id detection or turn counting.
    """
    if not isinstance(messages, list):
        return False
    for m in messages:
        if not isinstance(m, dict):
            return False
        if m.get("role") not in ("user", "assistant"):
            return False
        content = m.get("content")
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                return False
            if not isinstance(block.get("type"), str):
                return False
    return True
