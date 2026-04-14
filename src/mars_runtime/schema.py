"""Pydantic schema for `agent.yaml` — the declarative unit of a Mars daemon.

v2: the runtime is Mars itself (no more `claude -p` / `codex` subprocess).
Fields removed: `runtime`, `mcps`. Field added: `model`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr = Field(
        ...,
        description=(
            "Daemon identifier. Lowercase alnum + hyphens, Fly.io-app-name safe."
        ),
    )
    description: NonEmptyStr = Field(
        ..., description="Short human-readable summary."
    )
    model: NonEmptyStr = Field(
        default="claude-opus-4-5",
        description="LLM model id passed to llm_client (e.g. claude-opus-4-5, claude-sonnet-4-5).",
    )
    max_tokens: int = Field(
        default=8192,
        ge=1,
        description=(
            "Max output tokens per LLM call. Model-specific caps are not "
            "enforced here — the provider rejects oversized values. Keep the "
            "schema agnostic so it works across Anthropic / OpenAI / etc."
        ),
    )
    system_prompt_path: NonEmptyStr = Field(
        ..., description="Path to the daemon's system prompt file (e.g. CLAUDE.md)."
    )
    env: list[NonEmptyStr] = Field(
        default_factory=list,
        description="Env var names to forward from the machine's secret store.",
    )
    tools: list[NonEmptyStr] = Field(
        default_factory=list,
        description="Allowed tool names (empty = all registered tools).",
    )
    workdir: NonEmptyStr = Field(
        default="/workspace",
        description="Absolute working directory where the daemon runs.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.fullmatch(v):
            raise ValueError(
                "name must be lowercase alnum + hyphens (Fly.io app name format), "
                f"got {v!r}"
            )
        return v

    @field_validator("workdir")
    @classmethod
    def _validate_workdir_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"workdir must be absolute, got {v!r}")
        return v

    @field_validator("env")
    @classmethod
    def _validate_env_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _ENV_RE.fullmatch(name):
                raise ValueError(
                    "env entries must be POSIX shell env var names "
                    f"(uppercase alnum + underscore), got {name!r}"
                )
        return v

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> "AgentConfig":
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError(f"agent.yaml at {p} is empty")
        if not isinstance(data, dict):
            raise ValueError(
                f"agent.yaml at {p} must be a YAML mapping, got {type(data).__name__}"
            )
        return cls.model_validate(data)
