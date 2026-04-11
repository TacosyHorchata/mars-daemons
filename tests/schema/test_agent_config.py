"""Unit tests for the `AgentConfig` schema.

Covers: happy-path parsing of both example files + the invariants that
Epic 1 (supervisor) will rely on when reading a parsed config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from schema.agent import AgentConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"


def test_pr_reviewer_example_parses():
    cfg = AgentConfig.parse_file(EXAMPLES / "pr-reviewer-agent.yaml")

    assert cfg.name == "pr-reviewer"
    assert cfg.runtime == "claude-code"
    assert cfg.system_prompt_path == "./CLAUDE.md"
    assert cfg.workdir == "/workspace/pr-reviewer"
    assert "github" in cfg.mcps
    assert "Bash" in cfg.tools
    assert "GITHUB_TOKEN" in cfg.env


def test_orion_example_parses():
    cfg = AgentConfig.parse_file(EXAMPLES / "orion-daemon.yaml")

    assert cfg.name == "orion-ops"
    assert cfg.runtime == "claude-code"
    assert cfg.workdir == "/workspace/orion"
    assert cfg.mcps == ["whatsapp", "zoho"]
    assert "ZOHO_API_KEY" in cfg.env


def test_runtime_defaults_to_claude_code(tmp_path: Path):
    """Default runtime keeps single-source-of-truth for v1."""
    p = tmp_path / "minimal.yaml"
    p.write_text(
        "\n".join(
            [
                "name: minimal",
                "description: minimal daemon for schema default check",
                "system_prompt_path: ./CLAUDE.md",
            ]
        )
    )
    cfg = AgentConfig.parse_file(p)
    assert cfg.runtime == "claude-code"
    assert cfg.mcps == []
    assert cfg.env == []
    assert cfg.tools == []
    assert cfg.workdir == "/workspace"


def test_unknown_runtime_rejected(tmp_path: Path):
    """Extra runtimes are not supported until Epic 3 adds Codex."""
    p = tmp_path / "bad-runtime.yaml"
    p.write_text(
        "\n".join(
            [
                "name: bad",
                "description: bad runtime",
                "runtime: magic-agent",
                "system_prompt_path: ./CLAUDE.md",
            ]
        )
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


def test_extra_fields_rejected(tmp_path: Path):
    """`extra=forbid` prevents schema drift — unknown keys fail loudly."""
    p = tmp_path / "extra.yaml"
    p.write_text(
        "\n".join(
            [
                "name: extra",
                "description: has an unknown field",
                "system_prompt_path: ./CLAUDE.md",
                "not_a_real_field: 42",
            ]
        )
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


def test_missing_required_field_rejected(tmp_path: Path):
    p = tmp_path / "missing.yaml"
    p.write_text(
        "\n".join(
            [
                "name: missing-desc",
                "system_prompt_path: ./CLAUDE.md",
            ]
        )
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


def test_empty_file_rejected(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="empty"):
        AgentConfig.parse_file(p)


def test_non_mapping_rejected(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        AgentConfig.parse_file(p)


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        AgentConfig.parse_file(tmp_path / "does-not-exist.yaml")


def test_examples_are_valid_yaml_top_level_mappings():
    """Guard against someone committing a YAML list or scalar by mistake."""
    for example in ("pr-reviewer-agent.yaml", "orion-daemon.yaml"):
        with (EXAMPLES / example).open() as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), f"{example} must be a top-level mapping"


# --- Stricter validators (prevent silent-failure modes in the supervisor) ---


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "agent.yaml"
    p.write_text(body)
    return p


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "   ",
        "PR-Reviewer",  # uppercase
        "pr reviewer",  # space
        "pr_reviewer",  # underscore
        "-leading-hyphen",
        "trailing-hyphen-",
        "a" * 31,  # too long for Fly.io
    ],
)
def test_name_rejects_non_slug(tmp_path: Path, bad_name: str):
    p = _write(
        tmp_path,
        f'name: "{bad_name}"\ndescription: d\nsystem_prompt_path: ./CLAUDE.md\n',
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


@pytest.mark.parametrize(
    "bad_env",
    ["lowercase", "has space", "1LEADING_DIGIT", "dash-name", ""],
)
def test_env_rejects_invalid_shell_var_names(tmp_path: Path, bad_env: str):
    p = _write(
        tmp_path,
        f"""name: ok
description: d
system_prompt_path: ./CLAUDE.md
env:
  - "{bad_env}"
""",
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


def test_workdir_must_be_absolute(tmp_path: Path):
    p = _write(
        tmp_path,
        "name: ok\ndescription: d\nsystem_prompt_path: ./CLAUDE.md\nworkdir: workspace/foo\n",
    )
    with pytest.raises(ValidationError, match="absolute"):
        AgentConfig.parse_file(p)


def test_whitespace_only_strings_rejected(tmp_path: Path):
    """name='   ' used to pass min_length=1. Regression guard."""
    p = _write(
        tmp_path,
        'name: "   "\ndescription: d\nsystem_prompt_path: ./CLAUDE.md\n',
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


def test_whitespace_only_list_element_rejected(tmp_path: Path):
    p = _write(
        tmp_path,
        """name: ok
description: d
system_prompt_path: ./CLAUDE.md
mcps:
  - "   "
""",
    )
    with pytest.raises(ValidationError):
        AgentConfig.parse_file(p)


def test_from_yaml_file_alias_matches_parse_file():
    """`from_yaml_file` is the preferred name; `parse_file` is the story-era alias."""
    a = AgentConfig.from_yaml_file(EXAMPLES / "pr-reviewer-agent.yaml")
    b = AgentConfig.parse_file(EXAMPLES / "pr-reviewer-agent.yaml")
    assert a == b
