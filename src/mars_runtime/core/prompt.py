"""Prompt assembly — ONE job: build the system prompt from layers."""

from __future__ import annotations

import logging
from typing import Any

from .config import get_config
from .providers import SkillDefinition, get_memory_provider, get_rules_provider, get_skills_provider

logger = logging.getLogger(__name__)

CORE_RUNTIME_RULES = """## Runtime Rules
Before executing tools, briefly explain what you're going to do.
After using tools, summarize the results.

## Error Reporting
When a tool returns an error, report the exact error message concisely.
Do not apologize, suggest retries, or speculate about causes.

## Files
If the user attaches files, use the appropriate registered tool to process them.
Files are available as indexed artifacts (artifact 0, artifact 1, etc.)

## Parallelism
Use whichever tools you need based on the conversation.
When processing multiple items, call multiple tools in parallel.

## Working Memory (scratchpad)
You have a persistent scratchpad that lives for the whole conversation. Its layout:

- `scratchpad.notes` — **your own curated memory**. You write to this via the `edit_memory` tool. Organize notes with nested dotted keys; suggested top-level groupings are `user_prefs.*`, `findings.*`, `decisions.*`, `corrections.*`, but the structure is free-form.
- `scratchpad._log` — audit trail of every note you've saved (with timestamps). Read-only.

Other tools may write additional keys to the scratchpad (extractions, classifications, etc.). The shape map in the system context shows what's available.

To read actual values call `read_memory(key="...")`. Examples:
- `read_memory(key="notes.user_prefs")` → everything you've remembered about user preferences
- `read_memory(key="extractions.inv_0")` → full structured result from a prior extraction

If the shape map is missing or too shallow, start by calling `read_memory()` with no key to see the full shape, then drill in from what you see.

**When to call `edit_memory`** (do it eagerly, don't wait to be told):
1. **User preferences** — the user states a preference. Example: `edit_memory(key="notes.user_prefs.units", value="metric", why="user explicitly stated")`.
2. **Corrections** — the user corrects your output. Save so you don't repeat. Example: `edit_memory(key="notes.corrections.item_x", value="A-101", why="user corrected from A-100")`.
3. **Findings** — a cross-turn insight that matters.
4. **Decisions** — a choice you committed to that later turns depend on.

Every `edit_memory` call REQUIRES a `why` — if you cannot justify saving the note in one sentence, don't save it. Keep notes high-signal.

**When NOT to call `edit_memory`**:
- Ephemeral turn state (what you're about to do next) — that's in the conversation already.
- Raw tool outputs — they already live in the scratchpad. Reference them by key instead of inlining.
- Things the user hasn't actually stated — don't over-infer.

## Skills
Skills are instruction sets you can activate via `use_skill`. Available skills are listed below (if any). Use `use_skill(name="...", action="activate")` to activate, `use_skill(name="...", action="deactivate")` to deactivate.
"""


async def build_system_prompt(agent_prompt: str, org_id: str, agent_id: str | None = None) -> str:
    """Assemble the full system prompt from 5 layers."""
    config = get_config()
    base = config.base_prompt.strip() if config.base_prompt else CORE_RUNTIME_RULES
    parts = [base]

    rules_text = await _load_rules_text(org_id, agent_id)
    if rules_text:
        parts.append(rules_text)

    memories_text = await _load_memories_text(org_id, agent_id)
    if memories_text:
        parts.append(memories_text)

    skill_catalog = await _load_skill_catalog(org_id)
    if skill_catalog:
        parts.append(skill_catalog)

    if agent_prompt:
        parts.append(agent_prompt)

    return "\n\n".join(parts)


def build_active_skills_text(state: dict) -> str:
    active = state.get("active_skills", [])
    if not active:
        return ""

    config = get_config()
    max_chars = config.max_skill_tokens * 4

    parts = ["## Active Skills"]
    total_chars = 0
    for skill in active:
        prompt = skill["prompt"]
        args = skill.get("args", {})
        if args:
            try:
                prompt = prompt.format(**args)
            except (KeyError, IndexError):
                pass

        skill_text = f"### {skill['name']} ({skill['activation_mode']})\n{prompt}"
        if total_chars + len(skill_text) > max_chars:
            parts.append(f"### {skill['name']} — (truncated: token budget exceeded)")
            break
        parts.append(skill_text)
        total_chars += len(skill_text)

    return "\n\n".join(parts)


def expire_one_turn_skills(state: dict, *, skip_names: set[str] | None = None) -> None:
    """Remove one_turn skills that have already had their LLM iteration.

    `skip_names` is the set of skills that should be preserved even if they are
    `one_turn` — typically skills activated in the CURRENT iteration, so they
    survive to be consumed by the next LLM call. Without this, activating a
    one_turn skill via `use_skill` and immediately expiring it leaves the model
    with no knowledge of the skill on the next iteration.
    """
    active = state.get("active_skills", [])
    skip = skip_names or set()
    state["active_skills"] = [
        s for s in active
        if s.get("activation_mode") != "one_turn" or s["name"] in skip
    ]


# ─── Private helpers ───────────────────────────────────────────────

async def _load_rules_text(org_id: str, agent_id: str | None) -> str:
    provider = get_rules_provider()
    if provider is None:
        return ""
    try:
        rules = await provider.list_rules(org_id, agent_id)
        return _format_rules(rules)
    except Exception:
        logger.warning("agents_v2.prompt.rules_load_failed", exc_info=True)
        return ""


async def _load_memories_text(org_id: str, agent_id: str | None) -> str:
    if not agent_id:
        return ""
    provider = get_memory_provider()
    if provider is None:
        return ""
    try:
        memories = await provider.load_memories(org_id, agent_id)
        return _format_memories(memories)
    except Exception:
        logger.warning("agents_v2.prompt.memories_load_failed", exc_info=True)
        return ""


async def _load_skill_catalog(org_id: str) -> str:
    provider = get_skills_provider()
    if provider is None:
        return ""
    try:
        skills = await provider.list_skills(org_id)
        return _build_skill_catalog(skills)
    except Exception:
        logger.warning("agents_v2.prompt.skills_load_failed", exc_info=True)
        return ""


def _format_rules(rules: list[dict[str, Any]]) -> str:
    if not rules:
        return ""
    parts = ["## Rules"]
    for rule in rules:
        name = rule.get("name", "")
        content = rule.get("content", "")
        if content:
            parts.append(f"### {name}\n{content}" if name else content)
    return "\n\n".join(parts)


def _format_memories(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    parts = ["## Cross-Conversation Memories"]
    for mem in memories:
        key = mem.get("key", "")
        value = mem.get("value", "")
        parts.append(f"- **{key}**: {value}")
    return "\n\n".join(parts)


def _build_skill_catalog(skills: list[SkillDefinition]) -> str:
    if not skills:
        return ""
    parts = ["## Available Skills"]
    for skill in skills:
        mode_tag = f"[{skill.activation_mode}]" if skill.activation_mode else ""
        parts.append(f"- **{skill.name}** {mode_tag}: {skill.description}")
    return "\n\n".join(parts)
