"""Agent engine configuration.

All tunables with sensible defaults. Override via configure() or setup_agents().
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentConfig:
    # LLM
    model: str = "azure_ai/Kimi-K2.5"
    # Optional explicit credentials — forwarded to litellm.acompletion() when set.
    # Use these for OpenAI-compatible endpoints (Azure AI Foundry /openai/v1/,
    # Moonshot, DeepSeek, etc.) where the default provider env vars would collide
    # with a different OpenAI tenant.
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.3
    llm_timeout_seconds: float = 60.0
    summarization_timeout_seconds: float = 30.0
    title_timeout_seconds: float = 15.0

    # Loop guards
    max_tool_calls_per_turn: int = 50

    # Context pruning
    token_threshold: int = 30_000
    keep_last_turns: int = 5
    max_persisted_messages: int = 200
    max_durable_event_history: int = 200

    # Skills
    max_active_skills: int = 3
    max_skill_tokens: int = 2_000

    # Base prompt
    base_prompt: str = ""

    # Messages (configurable per deployment)
    tool_limit_message: str = "Tool call limit reached for this turn. Continue in a new message."
    title_instruction: str = "Generate a concise title (≤50 chars) for this exchange. Return ONLY the title."
    summarizer_instruction: str = (
        "You are a conversation summarizer. Preserve: key facts, "
        "classification results, user decisions, file references, tool results. Be concise."
    )


_config: AgentConfig | None = None


def get_config() -> AgentConfig:
    global _config
    if _config is None:
        _config = AgentConfig()
    return _config


def configure(config: AgentConfig) -> None:
    global _config
    _config = config


def reset_config() -> None:
    global _config
    _config = None
