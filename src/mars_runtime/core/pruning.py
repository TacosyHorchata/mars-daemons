"""Context size management — ONE job: prune, estimate, title, error messages."""

from __future__ import annotations

import asyncio
import logging

import litellm

from .config import get_config
from .store import UsageMetrics

logger = logging.getLogger(__name__)


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(m.get("content", "") or "") for m in messages) // 4


def _split_into_turns(messages: list[dict]) -> list[list[dict]]:
    turns: list[list[dict]] = []
    current: list[dict] = []
    for msg in messages:
        if msg.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns


async def _summarize_messages(messages: list[dict], state: dict | None = None) -> str:
    config = get_config()
    transcript = "\n".join(
        f"[{m.get('role', '?')}]: {(m.get('content', '') or '')[:500]}"
        for m in messages
    )

    try:
        extra = {}
        if config.api_key: extra["api_key"] = config.api_key
        if config.api_base: extra["api_base"] = config.api_base
        async with asyncio.timeout(config.summarization_timeout_seconds):
            response = await litellm.acompletion(
                model=config.model,
                messages=[
                    {"role": "system", "content": config.summarizer_instruction},
                    {"role": "user", "content": f"Summarize this conversation:\n\n{transcript}"},
                ],
                max_tokens=2000,
                **extra,
            )
    except TimeoutError as exc:
        raise RuntimeError(
            f"Conversation summarization timed out after {config.summarization_timeout_seconds:.1f} seconds",
        ) from exc

    if state and response.usage:
        metrics: UsageMetrics = state.get("usage", UsageMetrics())
        metrics.input_tokens += response.usage.prompt_tokens or 0
        metrics.output_tokens += response.usage.completion_tokens or 0
        metrics.llm_calls += 1

    return response.choices[0].message.content or ""


async def prune_messages(state: dict) -> list[dict]:
    config = get_config()
    messages = state["messages"]
    if estimate_tokens(messages) <= config.token_threshold:
        return messages

    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    remaining = messages[1:] if system_msg else messages

    turns = _split_into_turns(remaining)
    if len(turns) <= config.keep_last_turns:
        return messages

    keep_turns = turns[-config.keep_last_turns:]
    prune_turns = turns[:-config.keep_last_turns]

    to_summarize = [m for turn in prune_turns for m in turn]
    summary = await _summarize_messages(to_summarize, state)

    summary_msg = {"role": "system", "content": f"[CONVERSATION SUMMARY]\n{summary}\n[END SUMMARY]"}
    kept_msgs = [m for turn in keep_turns for m in turn]

    result = []
    if system_msg:
        result.append(system_msg)
    result.append(summary_msg)
    result.extend(kept_msgs)
    return result


async def generate_title(conversation_id: str, state: dict, store) -> None:
    config = get_config()
    conversation = state.get("conversation", [])
    if not conversation:
        return

    user_msg = ""
    agent_msg = ""
    for entry in conversation:
        if entry.get("role") == "user" and not user_msg:
            user_msg = entry.get("content", "")
        elif entry.get("role") == "agent" and not agent_msg:
            agent_msg = entry.get("content", "")
        if user_msg and agent_msg:
            break

    if not user_msg:
        return

    org_id = state.get("org_id", "")

    try:
        extra = {}
        if config.api_key: extra["api_key"] = config.api_key
        if config.api_base: extra["api_base"] = config.api_base
        async with asyncio.timeout(config.title_timeout_seconds):
            response = await litellm.acompletion(
                model=config.model,
                messages=[
                    {"role": "system", "content": config.title_instruction},
                    {"role": "user", "content": f"User: {user_msg[:200]}\nAgent: {agent_msg[:200]}"},
                ],
                max_tokens=50,
                **extra,
            )
        title = (response.choices[0].message.content or "").strip()[:50]
        if not title:
            return
        await store.update_title(conversation_id, title, org_id=org_id)
    except TimeoutError:
        logger.warning("Auto-title generation timed out for %s", conversation_id)
    except Exception as e:
        logger.warning("Auto-title generation failed for %s: %s", conversation_id, e)


def build_turn_error_message(exc: BaseException) -> str:
    if exc.__class__.__name__ == "AgentTimeoutError":
        return str(exc)
    return "The agent could not complete this turn."


def ensure_turn_error_message(state: dict, content: str) -> dict:
    conversation = state.setdefault("conversation", [])
    if conversation:
        last_entry = conversation[-1]
        if (
            last_entry.get("role") == "agent"
            and last_entry.get("type") == "agent_message"
            and last_entry.get("error_kind") == "turn_error"
            and last_entry.get("content") == content
        ):
            return last_entry

    from .state import save_to_conversation
    save_to_conversation(state, "agent", content, "agent_message", error_kind="turn_error")
    return state["conversation"][-1]
