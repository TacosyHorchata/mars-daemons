"""Pydantic schema for `agent.yaml` — the declarative unit of a Mars daemon.

v1 rules (see docs/planning/epics/epic-00-foundation-and-spikes.md):
- Single concrete class, no Protocol, no inheritance.
- Two runtimes: `claude-code` (default) and `codex` (wired up in Epic 3).
- Lists of strings for `mcps`, `env`, `tools` — no nested config shapes yet.
- `parse_file()` loads YAML and validates in one call. (Pydantic v2 removed
  the built-in generic `parse_file`, so we re-expose it as a YAML-specific
  classmethod. `from_yaml_file()` is the preferred name going forward.)

Validation is strict on purpose: agent.yaml is a user-authored file that
drives subprocess spawning, secret forwarding, and filesystem layout inside
the machine. Silent-failure modes here (whitespace-only name, env var with a
space in it, relative workdir) all turn into hard-to-debug supervisor bugs.
Better to fail loudly at load time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Runtime = Literal["claude-code", "codex"]

# Fly.io app name rules are the strictest downstream consumer of `name`.
# Lowercase alnum + hyphen, must start and end alnum, max 30 chars for safety.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")

# POSIX shell env var convention — uppercase, digits, underscore, no leading digit.
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentConfig(BaseModel):
    """Declarative definition of a Mars daemon.

    Loaded from an `agent.yaml` file. Everything the control plane and the
    machine supervisor need to spawn, secure, and display a daemon is encoded
    here — no hidden defaults elsewhere.
    """

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr = Field(
        ...,
        description=(
            "Daemon identifier. Lowercase alnum + hyphens, Fly.io-app-name safe. "
            "Used as workspace key, path segment, and UI label."
        ),
    )
    description: NonEmptyStr = Field(
        ...,
        description="Short human-readable summary shown in the dashboard session list.",
    )
    runtime: Runtime = Field(
        default="claude-code",
        description="Which CLI runtime the supervisor spawns for this daemon.",
    )
    system_prompt_path: NonEmptyStr = Field(
        ...,
        description="Path to the daemon's system prompt file (usually CLAUDE.md or AGENTS.md).",
    )
    mcps: list[NonEmptyStr] = Field(
        default_factory=list,
        description="MCP server names enabled for this daemon (wired up by the supervisor).",
    )
    env: list[NonEmptyStr] = Field(
        default_factory=list,
        description="Env var names to forward from the machine's secret store into the subprocess.",
    )
    tools: list[NonEmptyStr] = Field(
        default_factory=list,
        description="Allowed tool names for permission filtering (empty = runtime default).",
    )
    workdir: NonEmptyStr = Field(
        default="/workspace",
        description="Absolute working directory inside the machine where the daemon runs.",
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
            raise ValueError(
                f"workdir must be an absolute path (supervisor interprets it as a "
                f"machine path), got {v!r}"
            )
        return v

    @field_validator("env")
    @classmethod
    def _validate_env_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _ENV_RE.fullmatch(name):
                raise ValueError(
                    "env entries must be POSIX shell env var names "
                    f"(uppercase alnum + underscore, no leading digit), got {name!r}"
                )
        return v

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> "AgentConfig":
        """Load and validate an `agent.yaml` file. Preferred name.

        Raises:
            FileNotFoundError: if the file does not exist.
            yaml.YAMLError: if the file is not valid YAML.
            pydantic.ValidationError: if the document does not match the schema.
            ValueError: if the YAML document is empty or not a mapping.
        """
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError(f"agent.yaml at {p} is empty")
        if not isinstance(data, dict):
            raise ValueError(
                f"agent.yaml at {p} must be a YAML mapping at the top level, "
                f"got {type(data).__name__}"
            )
        return cls.model_validate(data)

    # Alias kept because Story 0.2's acceptance criterion explicitly names
    # `parse_file`. Prefer `from_yaml_file` in new code — this method name
    # collides with legacy Pydantic v1 semantics and will be dropped in v2.
    @classmethod
    def parse_file(cls, path: str | Path) -> "AgentConfig":  # noqa: D401
        """Alias for :meth:`from_yaml_file`. Prefer the YAML-specific name."""
        return cls.from_yaml_file(path)
