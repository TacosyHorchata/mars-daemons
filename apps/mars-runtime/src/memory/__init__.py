"""Per-session memory capture for the Mars runtime.

v1 writes three JSONL files per session under ``/workspace/<session-id>/memory/``:

* ``session_history.jsonl`` — every Mars event in arrival order. The
  full durable + ephemeral stream so a post-hoc reviewer can
  reconstruct exactly what the daemon saw.
* ``tool_calls.jsonl`` — every ``ToolCall`` and paired ``ToolResult``,
  indexed by ``tool_use_id``. Denormalized for quick auditing without
  scanning the full history.
* ``claude_md_proposals.jsonl`` — any assistant utterance that looks
  like a suggestion to modify ``CLAUDE.md`` or ``AGENTS.md``. v1
  CAPTURES these; v1.1+ admin UI surfaces them for manual review.
  NEVER auto-applied.

See :mod:`memory.capture`.
"""
