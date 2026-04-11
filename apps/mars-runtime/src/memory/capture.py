"""Per-session memory collector.

One :class:`MemoryCapture` instance per live session. The supervisor's
event pump calls :meth:`MemoryCapture.record` for every Mars event it
yields; the capture routes the event to the appropriate JSONL files
and flushes to disk.

v1 philosophy (from Story 6.2 + v1 plan item 9):

* **Capture everything, apply nothing.** ``CLAUDE.md`` diff proposals
  are stored verbatim for admin review; the capture layer NEVER
  writes the proposed content back to the prompt file.
* **Disk budget is bounded.** Each session's memory directory has a
  soft cap (default 100 MB). When exceeded we stop appending and log
  — we do NOT truncate past events because downstream review
  depends on a complete log. If the cap fires, the session emits a
  warning event and the supervisor's operator can lift the limit
  on the affected machine.
* **No auto-sync.** ``memory/sync.py`` owns the periodic S3 upload
  (Story 6.3). This module just writes to the volume.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

from events.types import (
    MARS_EVENT_ADAPTER,
    AssistantChunk,
    AssistantText,
    MarsEventBase,
    ToolCall,
    ToolResult,
)

__all__ = [
    "DEFAULT_DISK_BUDGET_BYTES",
    "MemoryCapture",
    "extract_prompt_proposal",
]

_log = logging.getLogger("mars.memory")

#: Soft cap for a session's memory directory. 100 MB is chosen to
#: give a typical dogfood session (~hours) headroom while still
#: fitting 100 concurrent sessions on a 10 GB Fly volume.
DEFAULT_DISK_BUDGET_BYTES = 100 * 1024 * 1024

_HISTORY_FILE = "session_history.jsonl"
_TOOLS_FILE = "tool_calls.jsonl"
_PROPOSALS_FILE = "claude_md_proposals.jsonl"

#: Pattern that catches assistant utterances referencing the
#: admin-only prompt files. Used only as a *proposal detector* —
#: matched text is captured for review, NEVER applied.
_PROMPT_FILE_PATTERN = re.compile(r"\b(CLAUDE\.md|AGENTS\.md)\b")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_prompt_proposal(event: MarsEventBase) -> dict[str, Any] | None:
    """Return a proposal dict if the event looks like a prompt-edit suggestion.

    v1 heuristic: any ``AssistantText`` containing ``CLAUDE.md`` or
    ``AGENTS.md`` is captured. False positives (e.g. "I checked
    CLAUDE.md and it says X") are acceptable — admin review filters
    them out in v1.1. False negatives are the worry, so keep the
    pattern broad.
    """
    if not isinstance(event, AssistantText):
        return None
    if not _PROMPT_FILE_PATTERN.search(event.text):
        return None
    return {
        "session_id": event.session_id,
        "type": "claude_md_proposal",
        "detected_at": _utcnow_iso(),
        "message_id": event.message_id,
        "block_index": event.block_index,
        "text": event.text,
        "match": _PROMPT_FILE_PATTERN.findall(event.text),
    }


class MemoryCapture:
    """Writes per-session JSONL memory files.

    Not thread-safe. Designed for a single asyncio task (the
    supervisor's event pump) that calls :meth:`record` in event order.
    """

    def __init__(
        self,
        session_id: str,
        root_dir: str | Path,
        *,
        disk_budget_bytes: int = DEFAULT_DISK_BUDGET_BYTES,
    ) -> None:
        self._session_id = session_id
        self._root_dir = Path(root_dir) / session_id / "memory"
        self._disk_budget = disk_budget_bytes
        self._closed = False
        self._over_budget = False
        self._bytes_written = 0

        # Pending tool calls keyed by tool_use_id so we can pair
        # the ToolResult back to its ToolCall for tool_calls.jsonl.
        self._pending_tool_calls: dict[str, ToolCall] = {}

        self._history_fp: IO[str] | None = None
        self._tools_fp: IO[str] | None = None
        self._proposals_fp: IO[str] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> "MemoryCapture":
        """Create the session memory directory and open the JSONL files.

        Idempotent — calling twice is a no-op.
        """
        if self._history_fp is not None:
            return self
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._history_fp = (self._root_dir / _HISTORY_FILE).open("a", encoding="utf-8")
        self._tools_fp = (self._root_dir / _TOOLS_FILE).open("a", encoding="utf-8")
        self._proposals_fp = (self._root_dir / _PROPOSALS_FILE).open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        """Flush + close every JSONL file. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for fp in (self._history_fp, self._tools_fp, self._proposals_fp):
            if fp is None:
                continue
            try:
                fp.flush()
                fp.close()
            except Exception:  # noqa: BLE001
                _log.exception("memory capture close failed")
        self._history_fp = None
        self._tools_fp = None
        self._proposals_fp = None

    def __enter__(self) -> "MemoryCapture":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, event: MarsEventBase) -> None:
        """Append one event to the appropriate JSONL files.

        Silently drops the event if the session is over-budget.
        Drops with a logged warning if :meth:`open` was not called
        first (defensive — production code opens on construction).
        """
        if self._closed:
            return
        if self._history_fp is None:
            _log.warning("MemoryCapture.record called before open()")
            return
        if self._over_budget:
            return

        # Every event → session_history.jsonl
        line = json.dumps(
            MARS_EVENT_ADAPTER.dump_python(event, mode="json")
        )
        self._append(self._history_fp, line)

        # Tool accounting → tool_calls.jsonl
        if isinstance(event, ToolCall):
            self._pending_tool_calls[event.tool_use_id] = event
        elif isinstance(event, ToolResult):
            call = self._pending_tool_calls.pop(event.tool_use_id, None)
            pair = {
                "session_id": event.session_id,
                "tool_use_id": event.tool_use_id,
                "tool_name": call.tool_name if call is not None else None,
                "input": dict(call.input) if call is not None else None,
                "content": event.content,
                "is_error": event.is_error,
                "recorded_at": _utcnow_iso(),
            }
            self._append(self._tools_fp, json.dumps(pair))

        # Prompt-edit proposal detector → claude_md_proposals.jsonl
        proposal = extract_prompt_proposal(event)
        if proposal is not None:
            self._append(self._proposals_fp, json.dumps(proposal))

        if self._bytes_written > self._disk_budget and not self._over_budget:
            self._over_budget = True
            _log.warning(
                "memory capture for session %s exceeded %d byte budget at %d bytes — "
                "further events dropped until session end",
                self._session_id,
                self._disk_budget,
                self._bytes_written,
            )

    def _append(self, fp: IO[str] | None, line: str) -> None:
        if fp is None:
            return
        fp.write(line + "\n")
        fp.flush()
        self._bytes_written += len(line) + 1

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def over_budget(self) -> bool:
        return self._over_budget

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def pending_tool_calls(self) -> int:
        """How many ToolCall events are still waiting for their
        matching ToolResult. Useful in tests + for "how many
        pending tools" dashboards."""
        return len(self._pending_tool_calls)
