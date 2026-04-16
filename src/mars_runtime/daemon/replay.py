from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[session_id] = lock
        return lock


def _log_path(events_dir: Path, session_id: str) -> Path:
    return events_dir / f"{session_id}.jsonl"


def _next_sequence(path: Path) -> int:
    if not path.exists():
        return 1
    # Fast path: read last line without loading the whole file for small logs.
    # Since this MVP is append-only and bounded by turn count, simple scan is fine.
    last = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                seq = obj.get("sequence")
                if isinstance(seq, int) and seq > last:
                    last = seq
    return last + 1


def append_event(events_dir: Path, session_id: str, event: dict[str, Any]) -> int:
    events_dir.mkdir(parents=True, exist_ok=True)
    path = _log_path(events_dir, session_id)
    lock = _lock_for(session_id)
    with lock:
        seq = _next_sequence(path)
        payload = {"sequence": seq, "timestamp": time.time(), **event}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    return seq


def replay_after(events_dir: Path, session_id: str, after: int) -> list[dict[str, Any]]:
    path = _log_path(events_dir, session_id)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            seq = obj.get("sequence")
            if isinstance(seq, int) and seq > after:
                out.append(obj)
    return out
