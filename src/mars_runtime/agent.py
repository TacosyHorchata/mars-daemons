"""The agent loop.

Outer loop: read user turns from stdin (one line = one turn).
Inner loop: call LLM, execute tool_use blocks, loop until no tool_use.

See https://www.mihaileric.com/The-Emperor-Has-No-Clothes/ for the pattern.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TextIO

from . import session_store, workspace
from .events import emit
from .llm_client import LLMClient, Message
from .schema import AgentConfig
from .tools import ToolRegistry


def _read_turns(stream: TextIO) -> Iterator[str]:
    """Yield one user turn per non-empty stdin line.

    Blocking reads. EOF (Ctrl+D or stdin close) ends the outer loop.
    """
    for raw in stream:
        line = raw.rstrip("\n")
        if line:
            yield line


# Hard cap on consecutive tool-call iterations within a single user turn.
# Each iteration is a full paid roundtrip with the whole transcript resent,
# so the default is cost-conscious. Well-behaved agents rarely exceed 20.
MAX_TOOL_ITERATIONS = 24


def run(
    config: AgentConfig,
    llm: LLMClient,
    tools: ToolRegistry,
    *,
    stdin: TextIO | None = None,
    turn_source: Iterable[str] | None = None,
    sessions_dir: Path | None = None,
    session_id: str | None = None,
    workspace_path: Path | None = None,
    start_messages: list[Message] | None = None,
) -> None:
    system = Path(config.system_prompt_path).read_text(encoding="utf-8")
    messages: list[Message] = list(start_messages) if start_messages else []

    persist_args = (sessions_dir is not None, session_id is not None)
    if any(persist_args) and not all(persist_args):
        raise ValueError(
            "sessions_dir and session_id must both be provided or both be None"
        )
    persist = all(persist_args)
    do_git = workspace_path is not None

    if do_git:
        workspace.init_if_needed(workspace_path)

    emit(
        "session_started",
        name=config.name,
        model=config.model,
        cwd=config.workdir,
        tools=tools.names(),
        session_id=session_id,
    )

    # Turn numbering: each user turn → one commit if files changed. Count
    # existing user-role text messages so resumed sessions keep advancing.
    turn_number = sum(1 for m in messages if m["role"] == "user" and _is_user_text(m)) + 1

    # Turn source precedence: explicit iterable (worker RPC mode) > stdin
    # (legacy / tests) > sys.stdin (default).
    if turn_source is not None:
        turns: Iterable[str] = turn_source
    else:
        turns = _read_turns(stdin if stdin is not None else sys.stdin)
    stop_reason: str | None = None

    def _persist_turn(preview: str) -> None:
        # Commit first, then snapshot — so if git fails, the session file
        # does not silently advance past the last durable commit. If the
        # snapshot fails after the commit, on next resume the agent sees
        # messages from turn N-1 and a workspace at turn N. That mismatch
        # is recoverable (agent re-does work) rather than corrupting the
        # audit trail.
        if do_git:
            sha = workspace.commit_turn(workspace_path, turn_number, preview)  # type: ignore[arg-type]
            if sha:
                emit("turn_committed", commit_sha=sha, turn_number=turn_number)
        if persist:
            session_store.save(
                sessions_dir,  # type: ignore[arg-type]
                session_id,  # type: ignore[arg-type]
                config.name,
                config.model_dump(),
                messages,
            )
            emit("session_saved", session_id=session_id)

    try:
        for user_text in turns:
            emit("user_input", text=user_text)
            # Snapshot history length so we can roll back the whole turn
            # cleanly if we need to abort mid-flight. Rolling back just the
            # last message is wrong after iteration 0 (messages[-1] is a
            # tool_result, not the user turn).
            turn_start = len(messages)
            messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})

            for iteration in range(MAX_TOOL_ITERATIONS):
                # Consume the stream: emit assistant_chunk per text delta
                # for real-time UX, capture the final Response at
                # message_stop. Non-streaming clients subscribe to
                # assistant_text (still emitted below) or just wait for
                # turn_completed.
                #
                # If the stream raises mid-flight (broker chat_error,
                # network drop, SDK exception), roll back this turn and
                # abort cleanly instead of letting the exception escape
                # to worker main and kill the whole session.
                resp = None
                try:
                    for chunk in llm.chat_stream(
                        system=system,
                        messages=messages,
                        tools=tools.specs(),
                        model=config.model,
                        max_tokens=config.max_tokens,
                    ):
                        if chunk.kind == "text_delta":
                            emit("assistant_chunk", delta=chunk.text)
                        elif chunk.kind == "message_stop":
                            resp = chunk.final_response
                except Exception as e:
                    emit(
                        "turn_aborted",
                        reason="chat_stream_error",
                        detail=f"{type(e).__name__}: {e}",
                        iteration=iteration,
                    )
                    del messages[turn_start:]
                    break
                if resp is None:
                    # Provider returned no final_response — treat as an
                    # empty turn rather than crash the loop.
                    emit("turn_aborted", reason="no_final_response", iteration=iteration)
                    del messages[turn_start:]
                    break
                stop_reason = resp.stop_reason

                # Detect duplicate tool_use_ids BEFORE appending the assistant
                # turn. If we kept the duplicates in history, the next API
                # call would have N tool_use blocks for one id but only 1
                # tool_result — Anthropic rejects that as malformed. Safest:
                # abort the turn without polluting message history.
                ids_seen: set[str] = set()
                has_duplicates = False
                for call in resp.tool_calls:
                    if call.id in ids_seen:
                        has_duplicates = True
                        break
                    ids_seen.add(call.id)
                if has_duplicates:
                    emit("turn_aborted", reason="duplicate_tool_use_id", iteration=iteration)
                    # Roll back everything added during this turn so history
                    # stays valid. Truncating to turn_start drops the user
                    # turn plus any assistant/tool_result pairs we appended
                    # during the inner loop. No persist/commit — the rollback
                    # means this turn didn't happen.
                    del messages[turn_start:]
                    break

                messages.append({"role": "assistant", "content": resp.raw_content})

                if resp.text:
                    emit("assistant_text", text=resp.text)

                if stop_reason == "max_tokens":
                    emit("turn_truncated", stop_reason=stop_reason, iteration=iteration)

                if not resp.tool_calls:
                    emit("turn_completed", stop_reason=stop_reason)
                    _persist_turn(user_text[:60])
                    turn_number += 1
                    break

                tool_result_blocks: list[dict] = []
                for call in resp.tool_calls:
                    emit("tool_call", id=call.id, name=call.name, input=call.input)
                    output = tools.execute(call.name, call.input)
                    emit(
                        "tool_result",
                        id=call.id,
                        name=call.name,
                        content=output.content,
                        is_error=output.is_error,
                    )
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": output.content,
                            "is_error": output.is_error,
                        }
                    )
                messages.append({"role": "user", "content": tool_result_blocks})
            else:
                # Loop exhausted MAX_TOOL_ITERATIONS. Messages were not
                # rolled back — memory reflects the incomplete turn, so
                # persist it (mirror in-memory semantics).
                emit(
                    "turn_aborted",
                    reason="max_tool_iterations",
                    limit=MAX_TOOL_ITERATIONS,
                )
                _persist_turn(user_text[:60])
                turn_number += 1
    finally:
        emit("session_ended", stop_reason=stop_reason)


def _is_user_text(message: Message) -> bool:
    """True if this is a user turn (text input), not a tool_result batch."""
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(block.get("type") == "text" for block in content)
