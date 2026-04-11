"""Unit tests for :class:`memory.capture.MemoryCapture` (Story 6.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from events.types import (
    AssistantChunk,
    AssistantText,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
)
from memory.capture import (
    DEFAULT_DISK_BUDGET_BYTES,
    MemoryCapture,
    extract_prompt_proposal,
)

SESSION_ID = "mars-mem-1"


def _capture(tmp_path: Path, **kwargs) -> MemoryCapture:
    cap = MemoryCapture(SESSION_ID, tmp_path, **kwargs)
    cap.open()
    return cap


def _started() -> SessionStarted:
    return SessionStarted(
        session_id=SESSION_ID,
        model="claude-opus-4-6",
        cwd="/workspace",
        claude_code_version="2.1.101",
    )


def _call(tool_use_id: str = "tu-1") -> ToolCall:
    return ToolCall(
        session_id=SESSION_ID,
        tool_use_id=tool_use_id,
        tool_name="Bash",
        input={"command": "echo hi"},
    )


def _result(tool_use_id: str = "tu-1") -> ToolResult:
    return ToolResult(
        session_id=SESSION_ID,
        tool_use_id=tool_use_id,
        content="hi\n",
        is_error=False,
    )


# ---------------------------------------------------------------------------
# Directory layout + file creation
# ---------------------------------------------------------------------------


def test_open_creates_session_memory_dir_and_files(tmp_path: Path):
    cap = _capture(tmp_path)
    try:
        memdir = tmp_path / SESSION_ID / "memory"
        assert memdir.is_dir()
        for name in (
            "session_history.jsonl",
            "tool_calls.jsonl",
            "claude_md_proposals.jsonl",
        ):
            assert (memdir / name).exists()
    finally:
        cap.close()


def test_open_is_idempotent(tmp_path: Path):
    cap = _capture(tmp_path)
    cap.open()  # second open — no-op
    cap.close()
    cap.close()  # second close — no-op


def test_context_manager_opens_and_closes(tmp_path: Path):
    with MemoryCapture(SESSION_ID, tmp_path) as cap:
        cap.record(_started())
    assert (tmp_path / SESSION_ID / "memory" / "session_history.jsonl").exists()


# ---------------------------------------------------------------------------
# session_history.jsonl — every event in order
# ---------------------------------------------------------------------------


def test_record_writes_every_event_to_session_history(tmp_path: Path):
    cap = _capture(tmp_path)
    try:
        cap.record(_started())
        cap.record(_call())
        cap.record(_result())
        cap.record(AssistantText(session_id=SESSION_ID, text="done"))
        cap.record(
            SessionEnded(
                session_id=SESSION_ID,
                result="done",
                stop_reason="end_turn",
            )
        )
    finally:
        cap.close()

    lines = (tmp_path / SESSION_ID / "memory" / "session_history.jsonl").read_text().splitlines()
    assert len(lines) == 5
    types = [json.loads(line)["type"] for line in lines]
    assert types == [
        "session_started",
        "tool_call",
        "tool_result",
        "assistant_text",
        "session_ended",
    ]


def test_session_history_preserves_event_payloads(tmp_path: Path):
    cap = _capture(tmp_path)
    try:
        cap.record(_call(tool_use_id="tu-xyz"))
    finally:
        cap.close()

    line = (tmp_path / SESSION_ID / "memory" / "session_history.jsonl").read_text().strip()
    payload = json.loads(line)
    assert payload["type"] == "tool_call"
    assert payload["tool_use_id"] == "tu-xyz"
    assert payload["input"] == {"command": "echo hi"}


# ---------------------------------------------------------------------------
# tool_calls.jsonl — pair ToolCall + ToolResult by tool_use_id
# ---------------------------------------------------------------------------


def test_tool_call_paired_with_matching_result(tmp_path: Path):
    cap = _capture(tmp_path)
    try:
        cap.record(_call("tu-A"))
        cap.record(_call("tu-B"))
        cap.record(_result("tu-A"))
        cap.record(_result("tu-B"))
    finally:
        cap.close()

    lines = (tmp_path / SESSION_ID / "memory" / "tool_calls.jsonl").read_text().splitlines()
    assert len(lines) == 2
    pairs = [json.loads(line) for line in lines]
    by_id = {p["tool_use_id"]: p for p in pairs}
    assert by_id["tu-A"]["tool_name"] == "Bash"
    assert by_id["tu-A"]["input"] == {"command": "echo hi"}
    assert by_id["tu-A"]["content"] == "hi\n"
    assert by_id["tu-A"]["is_error"] is False
    assert "tu-B" in by_id


def test_tool_result_without_matching_call_is_still_recorded(tmp_path: Path):
    """Defensive: an orphan ToolResult (supervisor dropped the ToolCall
    somehow) should still show up in tool_calls.jsonl — losing it would
    make audit impossible."""
    cap = _capture(tmp_path)
    try:
        cap.record(_result("tu-orphan"))
    finally:
        cap.close()

    lines = (tmp_path / SESSION_ID / "memory" / "tool_calls.jsonl").read_text().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["tool_use_id"] == "tu-orphan"
    assert payload["tool_name"] is None
    assert payload["input"] is None


def test_pending_tool_calls_tracked(tmp_path: Path):
    cap = _capture(tmp_path)
    try:
        cap.record(_call("tu-1"))
        assert cap.pending_tool_calls == 1
        cap.record(_call("tu-2"))
        assert cap.pending_tool_calls == 2
        cap.record(_result("tu-1"))
        assert cap.pending_tool_calls == 1
        cap.record(_result("tu-2"))
        assert cap.pending_tool_calls == 0
    finally:
        cap.close()


# ---------------------------------------------------------------------------
# claude_md_proposals.jsonl — capture prompt-edit suggestions, never apply
# ---------------------------------------------------------------------------


def test_extract_prompt_proposal_detects_claude_md_mention():
    ev = AssistantText(
        session_id=SESSION_ID,
        text="I should update CLAUDE.md to clarify the style guide.",
    )
    proposal = extract_prompt_proposal(ev)
    assert proposal is not None
    assert proposal["type"] == "claude_md_proposal"
    assert "CLAUDE.md" in proposal["match"]


def test_extract_prompt_proposal_detects_agents_md_mention():
    ev = AssistantText(
        session_id=SESSION_ID,
        text="The AGENTS.md file says X.",
    )
    proposal = extract_prompt_proposal(ev)
    assert proposal is not None
    assert "AGENTS.md" in proposal["match"]


def test_extract_prompt_proposal_ignores_unrelated_text():
    ev = AssistantText(session_id=SESSION_ID, text="Hello world")
    assert extract_prompt_proposal(ev) is None


def test_extract_prompt_proposal_ignores_partial_matches():
    """CLAUDE.markdown or claude.md (lowercase) should NOT match — we
    want the exact admin-only file names so the detector stays
    tight."""
    ev = AssistantText(
        session_id=SESSION_ID, text="check claude.md or CLAUDE.markdown"
    )
    assert extract_prompt_proposal(ev) is None


def test_extract_prompt_proposal_skips_non_assistant_text():
    """Tool calls that happen to mention CLAUDE.md in their input
    aren't "proposals" — they're tool invocations. Detector only
    fires on assistant utterances."""
    call = ToolCall(
        session_id=SESSION_ID,
        tool_use_id="tu-1",
        tool_name="Read",
        input={"file_path": "CLAUDE.md"},
    )
    assert extract_prompt_proposal(call) is None


def test_proposal_written_to_proposals_jsonl(tmp_path: Path):
    cap = _capture(tmp_path)
    try:
        cap.record(
            AssistantText(
                session_id=SESSION_ID,
                text="I want to update CLAUDE.md with a new rule.",
                message_id="msg-1",
                block_index=0,
            )
        )
    finally:
        cap.close()

    lines = (
        tmp_path / SESSION_ID / "memory" / "claude_md_proposals.jsonl"
    ).read_text().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "claude_md_proposal"
    assert payload["message_id"] == "msg-1"
    assert "CLAUDE.md" in payload["match"]


def test_proposals_are_captured_but_never_applied_to_prompt_file(tmp_path: Path):
    """Regression guard for v1 plan item 8: proposals are captured
    for admin review, never applied back to any CLAUDE.md file."""
    cap = _capture(tmp_path)
    try:
        cap.record(
            AssistantText(
                session_id=SESSION_ID,
                text="add 'be helpful' to CLAUDE.md",
            )
        )
    finally:
        cap.close()

    memdir = tmp_path / SESSION_ID / "memory"
    files = {p.name for p in memdir.iterdir()}
    # ONLY the three JSONL files — never a CLAUDE.md or anything that
    # could be mistaken for the prompt itself.
    assert files == {
        "session_history.jsonl",
        "tool_calls.jsonl",
        "claude_md_proposals.jsonl",
    }
    # And the proposal file is NOT named CLAUDE.md in any way
    assert not any("CLAUDE" in name for name in files)


# ---------------------------------------------------------------------------
# Ephemeral events still go through session_history
# ---------------------------------------------------------------------------


def test_ephemeral_chunks_are_captured_in_session_history(tmp_path: Path):
    """AssistantChunk is ephemeral at the SSE layer but the memory
    capture log keeps everything for replay/audit."""
    cap = _capture(tmp_path)
    try:
        cap.record(AssistantChunk(session_id=SESSION_ID, delta="he"))
        cap.record(AssistantChunk(session_id=SESSION_ID, delta="llo"))
    finally:
        cap.close()

    lines = (
        tmp_path / SESSION_ID / "memory" / "session_history.jsonl"
    ).read_text().splitlines()
    assert len(lines) == 2
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["assistant_chunk", "assistant_chunk"]


# ---------------------------------------------------------------------------
# Disk budget cap
# ---------------------------------------------------------------------------


def test_disk_budget_stops_writes_after_cap(tmp_path: Path):
    # Tiny budget → first event fits, second triggers over-budget
    cap = _capture(tmp_path, disk_budget_bytes=50)
    try:
        cap.record(_started())  # 500+ bytes after serialization
        assert cap.over_budget is True
        # Further events dropped silently
        initial_bytes = cap.bytes_written
        cap.record(AssistantText(session_id=SESSION_ID, text="lost"))
        assert cap.bytes_written == initial_bytes
    finally:
        cap.close()


def test_over_budget_flag_stays_set_until_close(tmp_path: Path):
    cap = _capture(tmp_path, disk_budget_bytes=10)
    try:
        cap.record(_started())
        assert cap.over_budget
        assert cap.over_budget  # still set
    finally:
        cap.close()


def test_default_disk_budget_is_100_mb():
    assert DEFAULT_DISK_BUDGET_BYTES == 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# Defensive guards
# ---------------------------------------------------------------------------


def test_record_before_open_logs_and_drops(tmp_path: Path, caplog):
    cap = MemoryCapture(SESSION_ID, tmp_path)
    with caplog.at_level("WARNING", logger="mars.memory"):
        cap.record(_started())
    # No file created because open() was never called
    assert not (tmp_path / SESSION_ID / "memory" / "session_history.jsonl").exists()
    assert any("before open" in r.message for r in caplog.records)


def test_record_after_close_is_noop(tmp_path: Path):
    cap = _capture(tmp_path)
    cap.record(_started())
    cap.close()
    # After close, subsequent records are dropped silently
    cap.record(AssistantText(session_id=SESSION_ID, text="post-close"))
    lines = (
        tmp_path / SESSION_ID / "memory" / "session_history.jsonl"
    ).read_text().splitlines()
    assert len(lines) == 1  # only the pre-close event
