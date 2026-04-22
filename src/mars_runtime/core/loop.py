"""The ReAct loop — ONLY run_turn + LLM streaming + tool dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import litellm

from .config import get_config
from .exceptions import OrgScopingError, serialize_exception_details
from .events import (
    EVENT_AGENT_MESSAGE,
    EVENT_AGENT_REASONING,
    EVENT_TOOL_COMPLETED,
    EVENT_TOOL_ERROR,
    EVENT_TOOL_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_ERROR,
    publish_chunk,
    publish_durable_event,
    publish_ephemeral,
)
from .state import (
    append_llm_message,
    init_state,
    inject_scratchpad,
    inject_user_message,
    mark_tool_call_finished,
    mark_tool_call_started,
    restore_state,
    save_to_conversation,
)
from .store import UsageMetrics, get_store
from .tools import (
    AuthContext,
    ToolResult,
    build_all_tool_definitions,
    drain_turn_cleanups,
    get_dynamic_tool_provider,
    get_tool_by_name,
    init_turn_cleanups,
    reset_dynamic_tools_for_turn,
    reset_turn_cleanups,
    set_dynamic_tools_for_turn,
)

logger = logging.getLogger(__name__)


class AgentTimeoutError(RuntimeError):
    """Raised when an LLM-related operation exceeds its configured time budget."""


def _log_turn(event: str, *, conversation_id: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"conversation_id": conversation_id, **fields})


# ─── LLM helpers ───────────────────────────────────────────────────

async def llm_call_streaming(
    messages: list[dict],
    tools: list[dict],
    conversation_id: str,
    state: dict | None = None,
) -> tuple[Any, bool]:
    config = get_config()
    if state is not None:
        state["_streaming_buffer"] = ""
    try:
        async with asyncio.timeout(config.llm_timeout_seconds):
            extra = {}
            if config.api_key: extra["api_key"] = config.api_key
            if config.api_base: extra["api_base"] = config.api_base
            _t_call = time.monotonic()
            # Cap per-item inspection to avoid pathological O(N) for large
            # pasted payloads (PDF text, JSON dumps). 32KB/item is enough
            # for a rough token estimate; anything larger we floor at cap.
            _CAP = 32768
            _msg_tokens = sum(min(len(str(m.get("content", ""))), _CAP) for m in messages) // 4
            _tool_tokens = sum(min(len(str(t)), _CAP) for t in (tools or [])) // 4
            logger.warning(
                "AGENTS_V2_TIMING llm.pre_call model=%s msgs=%d est_msg_tokens=%d est_tool_tokens=%d",
                config.model, len(messages), _msg_tokens, _tool_tokens,
            )
            stream = await litellm.acompletion(
                model=config.model,
                messages=messages,
                tools=tools if tools else None,
                temperature=config.temperature,
                stream=True,
                stream_options={"include_usage": True},
                **extra,
            )
            logger.warning(
                "AGENTS_V2_TIMING llm.acompletion_returned dt=%.3fs",
                time.monotonic() - _t_call,
            )

            chunks: list = []
            has_tools = False
            streamed_text = False
            _first_chunk_logged = False
            _t_stream_start = time.monotonic()
            # Accumulate partial tool_call state keyed by index, so we can
            # emit `agent_reasoning` events as soon as the model commits to
            # a tool name — gives the user live feedback during what would
            # otherwise be silent "Pensando" time (common with Kimi K2.5,
            # which rarely emits preamble text before a tool call).
            tool_preview_state: dict[int, dict[str, Any]] = {}

            async for chunk in stream:
                if not _first_chunk_logged:
                    _first_chunk_logged = True
                    logger.warning(
                        "AGENTS_V2_TIMING llm.first_chunk dt=%.3fs",
                        time.monotonic() - _t_stream_start,
                    )
                chunks.append(chunk)
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue
                # Reasoning models (Kimi K2.5, DeepSeek-R1, o1) put chain-of-thought
                # in `reasoning_content` instead of `content`. Surface it as a live
                # ephemeral event so the user sees the model is actively working —
                # otherwise the entire reasoning phase looks like a dead spinner.
                reasoning_delta = getattr(delta, "reasoning_content", None)
                if reasoning_delta:
                    await publish_ephemeral(
                        conversation_id,
                        EVENT_AGENT_REASONING,
                        state or {},
                        reasoning_delta=reasoning_delta,
                    )
                if delta.tool_calls:
                    has_tools = True
                    for tc in delta.tool_calls:
                        idx = getattr(tc, "index", 0) or 0
                        entry = tool_preview_state.setdefault(
                            idx,
                            {"name": None, "args": "", "name_emitted": False},
                        )
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            name_delta = getattr(fn, "name", None)
                            if name_delta and not entry["name"]:
                                entry["name"] = name_delta
                            args_delta = getattr(fn, "arguments", None)
                            if args_delta:
                                entry["args"] += args_delta
                        # Emit once per tool as soon as we know its name.
                        if entry["name"] and not entry["name_emitted"]:
                            entry["name_emitted"] = True
                            await publish_ephemeral(
                                conversation_id,
                                EVENT_AGENT_REASONING,
                                state or {},
                                tool_name=entry["name"],
                                tool_index=idx,
                            )
                if not has_tools and delta.content:
                    await publish_chunk(conversation_id, delta.content)
                    if state is not None:
                        state["_streaming_buffer"] = (
                            state.get("_streaming_buffer", "") + delta.content
                        )
                    streamed_text = True
    except TimeoutError as exc:
        raise AgentTimeoutError(
            f"LLM call timed out after {config.llm_timeout_seconds:.1f} seconds",
        ) from exc

    logger.warning(
        "AGENTS_V2_TIMING llm.stream_end dt=%.3fs chunks=%d has_tools=%s",
        time.monotonic() - _t_stream_start, len(chunks), has_tools,
    )
    _t_build = time.monotonic()
    full_response = litellm.stream_chunk_builder(chunks, messages=messages)
    logger.warning(
        "AGENTS_V2_TIMING llm.chunk_builder dt=%.3fs",
        time.monotonic() - _t_build,
    )
    return full_response, streamed_text


def has_tool_calls(response) -> bool:
    return bool(response.choices[0].message.tool_calls)


def extract_text_content(response) -> str | None:
    return response.choices[0].message.content


def format_assistant_message(response) -> dict:
    msg = response.choices[0].message
    result = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return result


def parse_tool_calls(response) -> list[dict]:
    actions = []
    for tc in response.choices[0].message.tool_calls:
        try:
            input_data = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            input_data = {"_raw": tc.function.arguments, "_parse_error": True}
        actions.append({
            "tool": tc.function.name,
            "call_id": tc.id,
            "input": input_data,
            "label": tc.function.name,
        })
    return actions


async def execute_single_tool(action: dict, auth: AuthContext, state: dict) -> ToolResult:
    tool = get_tool_by_name(action["tool"])
    if not tool:
        return ToolResult(
            success=False,
            error=f"Unknown tool: {action['tool']}",
            error_details={
                "error_type": "UnknownToolError",
                "error_message": f"Unknown tool: {action['tool']}",
                "traceback": None,
                "phase": "tool_lookup",
                "tool": action["tool"],
                "call_id": action["call_id"],
                "input": action["input"],
            },
        )
    return await tool.execute(action["input"], auth, state)


def track_llm_usage(response, state: dict) -> None:
    usage_obj = response.usage
    if usage_obj:
        metrics: UsageMetrics = state["usage"]
        metrics.input_tokens += usage_obj.prompt_tokens or 0
        metrics.output_tokens += usage_obj.completion_tokens or 0
        metrics.llm_calls += 1


def _normalize_tool_error_details(action: dict, result: ToolResult, *, phase: str) -> dict:
    details = dict(result.error_details or {})
    details.setdefault("error_type", "ToolExecutionError")
    details.setdefault("error_message", result.error or "Unknown error")
    details.setdefault("traceback", None)
    details["phase"] = phase
    details["tool"] = action["tool"]
    details["call_id"] = action["call_id"]
    details["input"] = action["input"]
    return details


def _update_state_with_tool_result(state: dict, action: dict, result: ToolResult) -> None:
    append_llm_message(state, {
        "role": "tool",
        "tool_call_id": action["call_id"],
        "name": action["tool"],
        "content": json.dumps(result.data if result.success else {"error": result.error}, default=str),
    })
    mark_tool_call_finished(
        state,
        action,
        success=result.success,
        output=result.data,
        error=result.error,
        error_details=result.error_details,
        tokens=result.tokens_used,
    )


async def _persist_tool_started(
    conversation_id: str,
    action: dict[str, Any],
    state: dict,
) -> dict[str, Any]:
    tool = get_tool_by_name(action["tool"])
    execution_mode = getattr(tool, "execution_mode", "parallel")
    started_entry = mark_tool_call_started(
        state,
        action,
        execution_mode=execution_mode,
    )
    await publish_durable_event(
        conversation_id,
        EVENT_TOOL_STARTED,
        state,
        tool=action["tool"],
        label=action.get("label", action["tool"]),
        call_id=action["call_id"],
        input=action["input"],
        status=started_entry["status"],
        started_at=started_entry["started_at"],
        execution_mode=started_entry.get("execution_mode"),
    )
    return started_entry


# ─── Core loop ─────────────────────────────────────────────────────

async def run_turn(
    conversation_id: str,
    agent_prompt: str,
    user_message: str,
    org_id: str,
    *,
    files: list[dict] | None = None,
    bearer_token: str | None = None,
    user_id: str | None = None,
    is_new_conversation: bool = False,
    agent_id: str | None = None,
) -> dict:
    from .prompt import build_active_skills_text, build_system_prompt, expire_one_turn_skills
    from .pruning import build_turn_error_message, ensure_turn_error_message, generate_title, prune_messages

    turn_start = time.monotonic()
    config = get_config()
    auth = AuthContext(org_id=org_id, user_id=user_id, bearer_token=bearer_token)
    state: dict = {}
    current_phase = "init"
    current_action: dict[str, Any] | None = None

    dynamic_token = None
    provider = get_dynamic_tool_provider()
    cleanup_token = init_turn_cleanups()

    try:
        if provider is not None:
            current_phase = "dynamic_tool_provider"
            dynamic_tools = await provider(org_id)
            if dynamic_tools:
                dynamic_token = set_dynamic_tools_for_turn(dynamic_tools)

        current_phase = "restore"
        store = get_store()
        persisted = await store.load(conversation_id, org_id=org_id)

        if persisted:
            system_prompt = await build_system_prompt(agent_prompt, org_id, agent_id)
            state = restore_state(persisted.context, system_prompt, org_id)
            state["usage"] = persisted.usage
        elif not is_new_conversation:
            raise OrgScopingError(
                f"Conversation {conversation_id} is not accessible for org {org_id}"
            )
        else:
            system_prompt = await build_system_prompt(agent_prompt, org_id, agent_id)
            state = init_state(system_prompt, org_id)

        state["conversation_id"] = conversation_id
        state["status"] = "working"
        state.setdefault("_durable_events", [])
        if agent_id:
            state["agent_id"] = agent_id

        current_phase = "inject_user_message"
        inject_user_message(state, user_message, files or [])

        tools = build_all_tool_definitions()
        tools_this_turn = 0

        # Freeze provider-backed prompt layers once per turn (rules, memories, skill catalog).
        # Only active skills text is recomputed inside the loop since skills can
        # activate/deactivate mid-turn. Re-fetching providers every iteration would
        # make behavior depend on provider uptime and could cause memory duplication.
        frozen_system_prompt = system_prompt

        _iter_count = 0
        while state["status"] == "working":
            _iter_count += 1
            _t_iter_start = time.monotonic()
            logger.warning(
                "AGENTS_V2_TIMING iter.start n=%d",
                _iter_count,
            )
            # Snapshot active skills at the START of the iteration so that
            # `expire_one_turn_skills` below can distinguish skills ACTIVATED
            # this iter (keep them alive for the next LLM call) from skills
            # that already had their turn (expire now). Without this, a skill
            # activated via `use_skill` in iter N was removed before iter N+1
            # could inject its instructions — the model then saw no skill in
            # the system prompt and hallucinated async behavior.
            _skills_at_iter_start = {
                s["name"] for s in state.get("active_skills", [])
            }
            if tools_this_turn >= config.max_tool_calls_per_turn:
                state["status"] = "idle"
                msg = config.tool_limit_message
                save_to_conversation(state, "agent", msg, "agent_message")
                await publish_durable_event(conversation_id, EVENT_AGENT_MESSAGE, state, content=msg)
                break

            # Recompute only the active skills overlay (local state, no provider calls)
            active_skills_text = build_active_skills_text(state)
            full_prompt = f"{frozen_system_prompt}\n\n{active_skills_text}" if active_skills_text else frozen_system_prompt
            if state["messages"] and state["messages"][0]["role"] == "system":
                state["messages"][0]["content"] = full_prompt

            inject_scratchpad(state)
            _t_prune = time.monotonic()
            state["messages"] = await prune_messages(state)
            logger.warning(
                "AGENTS_V2_TIMING prune dt=%.3fs msgs_after=%d",
                time.monotonic() - _t_prune, len(state["messages"]),
            )

            current_phase = "llm_call"
            _log_turn(
                "agents_v2.turn.llm_started",
                conversation_id=conversation_id,
                message_count=len(state["messages"]),
                tool_count=len(tools),
            )
            llm_start = time.monotonic()
            response, was_streamed = await llm_call_streaming(state["messages"], tools, conversation_id, state)
            llm_duration_ms = int((time.monotonic() - llm_start) * 1000)
            _log_turn(
                "agents_v2.turn.llm_completed",
                conversation_id=conversation_id,
                streamed=was_streamed,
                duration_ms=llm_duration_ms,
            )
            track_llm_usage(response, state)

            if has_tool_calls(response):
                text = extract_text_content(response)
                if text and text.strip():
                    save_to_conversation(state, "agent", text, "agent_message")
                    await publish_durable_event(conversation_id, EVENT_AGENT_MESSAGE, state, content=text)
                    state["_streaming_buffer"] = ""

                append_llm_message(state, format_assistant_message(response))

                current_phase = "parse_tool_calls"
                actions = parse_tool_calls(response)

                exclusive_action = next(
                    (a for a in actions
                     if (t := get_tool_by_name(a["tool"])) and t.execution_mode == "exclusive"),
                    None,
                )
                non_exclusive = [a for a in actions if a is not exclusive_action] if exclusive_action else []

                if exclusive_action:
                    current_phase = "exclusive.execute_tool"
                    current_action = exclusive_action
                    save_to_conversation(
                        state, "agent", "", "tool_started",
                        tool=exclusive_action["tool"],
                        label=exclusive_action.get("label", exclusive_action["tool"]),
                        call_id=exclusive_action["call_id"],
                    )
                    await _persist_tool_started(conversation_id, exclusive_action, state)

                    for skipped in non_exclusive:
                        append_llm_message(state, {
                            "role": "tool",
                            "tool_call_id": skipped["call_id"],
                            "name": skipped["tool"],
                            "content": json.dumps({"status": "skipped", "reason": "Awaiting exclusive tool"}),
                        })

                    result = await execute_single_tool(exclusive_action, auth, state)
                    if not result.success:
                        result.error_details = _normalize_tool_error_details(exclusive_action, result, phase=current_phase)
                    _update_state_with_tool_result(state, exclusive_action, result)
                    state["usage"].tool_calls += 1

                    if result.next_status:
                        state["status"] = result.next_status

                    _t_publish = time.monotonic()
                    if result.success:
                        await publish_durable_event(
                            conversation_id, EVENT_TOOL_COMPLETED, state,
                            tool=exclusive_action["tool"],
                            label=exclusive_action.get("label", exclusive_action["tool"]),
                            call_id=exclusive_action["call_id"],
                        )
                    else:
                        await publish_durable_event(
                            conversation_id, EVENT_TOOL_ERROR, state,
                            tool=exclusive_action["tool"],
                            call_id=exclusive_action["call_id"],
                            error_reason=result.error or "Unknown error",
                        )
                    logger.warning(
                        "AGENTS_V2_TIMING tool.post_publish dt=%.3fs tool=%s success=%s",
                        time.monotonic() - _t_publish, exclusive_action["tool"], result.success,
                    )
                    expire_one_turn_skills(state, skip_names={s["name"] for s in state.get("active_skills", [])} - _skills_at_iter_start)
                    # Continue the ReAct loop — the agent must get another LLM
                    # turn to interpret the tool result and either call more
                    # tools or produce the final user-facing text. Breaking here
                    # leaves status="working" but exits the loop, so the frontend
                    # never receives `turn_completed` and hangs on "Pensando".
                    # If the tool explicitly requested termination, next_status
                    # above already set status≠working and the while condition
                    # will naturally exit.
                    continue

                tools_this_turn += len(actions)
                current_phase = "execute_tools"

                for action in actions:
                    current_action = action
                    save_to_conversation(
                        state, "agent", "", "tool_started",
                        tool=action["tool"], label=action.get("label", action["tool"]),
                        call_id=action["call_id"],
                    )
                    await _persist_tool_started(conversation_id, action, state)

                tool_batch_start = time.monotonic()
                raw_results = await asyncio.gather(
                    *(execute_single_tool(a, auth, state) for a in actions),
                    return_exceptions=True,
                )
                tool_batch_duration_ms = int((time.monotonic() - tool_batch_start) * 1000)

                for action, result in zip(actions, raw_results):
                    current_action = action
                    if isinstance(result, BaseException):
                        result = ToolResult(
                            success=False,
                            error=str(result),
                            error_details=serialize_exception_details(
                                result,
                                phase=current_phase,
                                tool=action["tool"],
                                call_id=action["call_id"],
                                input_payload=action["input"],
                            ),
                        )
                    elif not result.success:
                        result.error_details = _normalize_tool_error_details(action, result, phase=current_phase)

                    _update_state_with_tool_result(state, action, result)
                    state["usage"].tool_calls += 1

                    if result.success:
                        for entry in reversed(state["conversation"]):
                            if entry.get("call_id") == action["call_id"] and entry.get("type") == "tool_started":
                                entry["type"] = "tool_completed"
                                break
                        await publish_durable_event(
                            conversation_id, EVENT_TOOL_COMPLETED, state,
                            tool=action["tool"], label=action.get("label", action["tool"]),
                            call_id=action["call_id"],
                        )
                    else:
                        for entry in reversed(state["conversation"]):
                            if entry.get("call_id") == action["call_id"] and entry.get("type") == "tool_started":
                                entry["type"] = "tool_error"
                                entry["error_reason"] = result.error or "Unknown error"
                                break
                        await publish_durable_event(
                            conversation_id, EVENT_TOOL_ERROR, state,
                            tool=action["tool"], call_id=action["call_id"],
                            error_reason=result.error or "Unknown error",
                        )

                expire_one_turn_skills(state, skip_names={s["name"] for s in state.get("active_skills", [])} - _skills_at_iter_start)

            else:
                text = extract_text_content(response) or ""

                if text.strip():
                    append_llm_message(state, {"role": "assistant", "content": text})
                    save_to_conversation(state, "agent", text, "agent_message")
                    await publish_durable_event(conversation_id, EVENT_AGENT_MESSAGE, state, content=text)
                    state["_streaming_buffer"] = ""

                    is_first_response = not persisted or not any(
                        e.get("type") == "agent_message"
                        for e in (persisted.context.conversation if persisted else [])
                    )
                    if is_first_response:
                        asyncio.ensure_future(generate_title(conversation_id, state, store))
                else:
                    append_llm_message(state, {"role": "assistant", "content": ""})

                expire_one_turn_skills(state)
                state["status"] = "idle"
                break

        if state["status"] in ("idle", "error"):
            current_phase = "finalize"
            turn_duration_ms = int((time.monotonic() - turn_start) * 1000)
            _log_turn(
                "agents_v2.turn.completed",
                conversation_id=conversation_id,
                final_status=state["status"],
                llm_calls=state["usage"].llm_calls,
                tool_calls=state["usage"].tool_calls,
                input_tokens=state["usage"].input_tokens,
                output_tokens=state["usage"].output_tokens,
                turn_duration_ms=turn_duration_ms,
            )
            await publish_durable_event(conversation_id, EVENT_TURN_COMPLETED, state)

        return state

    except OrgScopingError:
        raise
    except Exception as e:
        _log_turn(
            "agents_v2.turn.failed",
            conversation_id=conversation_id,
            failure_reason=str(e),
            phase=current_phase,
            level=logging.ERROR,
        )
        try:
            state["status"] = "error"
            state.setdefault("messages", [])
            state.setdefault("tool_calls", [])
            state.setdefault("conversation", [])
            state.setdefault("scratchpad", {})
            state.setdefault("files", [])
            state.setdefault("usage", UsageMetrics())
            state.setdefault("_event_sequence", 0)
            state.setdefault("_durable_events", [])

            partial_agent_message = state.get("_streaming_buffer") or ""

            user_safe_error = build_turn_error_message(e)
            ensure_turn_error_message(state, user_safe_error)
            turn_error_kwargs: dict[str, Any] = {"error": user_safe_error}
            if partial_agent_message:
                turn_error_kwargs["partial_agent_message"] = partial_agent_message
            await publish_durable_event(
                conversation_id, EVENT_TURN_ERROR, state, **turn_error_kwargs,
            )
        except Exception as publish_err:
            logger.error("Failed to publish error state for %s", conversation_id, exc_info=True)
            raise publish_err from e
        return state
    finally:
        try:
            await drain_turn_cleanups()
        finally:
            reset_turn_cleanups(cleanup_token)
            if dynamic_token is not None:
                reset_dynamic_tools_for_turn(dynamic_token)
