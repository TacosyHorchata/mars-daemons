"""The agent loop.

Outer loop: read user turns from stdin (one line = one turn).
Inner loop: call LLM, execute tool_use blocks, loop until no tool_use.

See https://www.mihaileric.com/The-Emperor-Has-No-Clothes/ for the pattern.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

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
) -> None:
    system = Path(config.system_prompt_path).read_text(encoding="utf-8")
    messages: list[Message] = []

    emit(
        "session_started",
        name=config.name,
        model=config.model,
        cwd=config.workdir,
        tools=tools.names(),
    )

    turns = _read_turns(stdin if stdin is not None else sys.stdin)
    stop_reason: str | None = None

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
                resp = llm.chat(
                    system=system,
                    messages=messages,
                    tools=tools.specs(),
                    model=config.model,
                    max_tokens=config.max_tokens,
                )
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
                    # during the inner loop.
                    del messages[turn_start:]
                    break

                messages.append({"role": "assistant", "content": resp.raw_content})

                if resp.text:
                    emit("assistant_text", text=resp.text)

                if stop_reason == "max_tokens":
                    emit("turn_truncated", stop_reason=stop_reason, iteration=iteration)

                if not resp.tool_calls:
                    emit("turn_completed", stop_reason=stop_reason)
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
                # Loop exhausted MAX_TOOL_ITERATIONS. Emit abort event. The
                # incomplete turn stays in history — future turns start from
                # a potentially messy state, which is acceptable for v0 but
                # worth revisiting if it causes regressions.
                emit(
                    "turn_aborted",
                    reason="max_tool_iterations",
                    limit=MAX_TOOL_ITERATIONS,
                )
    finally:
        emit("session_ended", stop_reason=stop_reason)
