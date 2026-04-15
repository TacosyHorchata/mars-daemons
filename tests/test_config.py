"""Unit tests for the v2 `AgentConfig` schema."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mars_runtime.config import AgentConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


def test_pr_reviewer_example_parses():
    cfg = AgentConfig.from_yaml_file(EXAMPLES / "pr-reviewer-agent.yaml")
    assert cfg.name == "pr-reviewer"
    assert cfg.model == "claude-opus-4-5"
    assert cfg.system_prompt_path == "./CLAUDE.md"
    assert cfg.workdir == "/workspace/pr-reviewer"
    assert "bash" in cfg.tools
    assert "GITHUB_TOKEN" in cfg.env


def test_defaults(tmp_path: Path):
    p = tmp_path / "minimal.yaml"
    p.write_text(
        "name: minimal\n"
        "description: minimal daemon for schema default check\n"
        "system_prompt_path: ./CLAUDE.md\n"
    )
    cfg = AgentConfig.from_yaml_file(p)
    assert cfg.model == "claude-opus-4-5"
    assert cfg.env == []
    assert cfg.tools == []
    assert cfg.workdir == "/workspace"
    assert cfg.provider is None  # None means "infer from model"


def test_provider_explicit(tmp_path: Path):
    p = tmp_path / "azure.yaml"
    p.write_text(
        "name: azure-agent\n"
        "description: uses Azure OpenAI\n"
        "system_prompt_path: ./CLAUDE.md\n"
        "model: my-gpt4-deployment\n"
        "provider: azure_openai\n"
    )
    cfg = AgentConfig.from_yaml_file(p)
    assert cfg.provider == "azure_openai"
    assert cfg.model == "my-gpt4-deployment"


def test_old_runtime_field_rejected(tmp_path: Path):
    """v1's `runtime:` field no longer exists; extra=forbid rejects it."""
    p = tmp_path / "legacy.yaml"
    p.write_text(
        "name: legacy\n"
        "description: has old runtime field\n"
        "runtime: claude-code\n"
        "system_prompt_path: ./CLAUDE.md\n"
    )
    with pytest.raises(ValidationError):
        AgentConfig.from_yaml_file(p)


def test_old_mcps_field_rejected(tmp_path: Path):
    p = tmp_path / "legacy.yaml"
    p.write_text(
        "name: legacy\n"
        "description: has old mcps field\n"
        "system_prompt_path: ./CLAUDE.md\n"
        "mcps: []\n"
    )
    with pytest.raises(ValidationError):
        AgentConfig.from_yaml_file(p)


def test_extra_fields_rejected(tmp_path: Path):
    p = tmp_path / "extra.yaml"
    p.write_text(
        "name: extra\n"
        "description: has an unknown field\n"
        "system_prompt_path: ./CLAUDE.md\n"
        "not_a_real_field: 42\n"
    )
    with pytest.raises(ValidationError):
        AgentConfig.from_yaml_file(p)


def test_missing_required_field_rejected(tmp_path: Path):
    p = tmp_path / "missing.yaml"
    p.write_text("name: missing-desc\nsystem_prompt_path: ./CLAUDE.md\n")
    with pytest.raises(ValidationError):
        AgentConfig.from_yaml_file(p)


def test_empty_file_rejected(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="empty"):
        AgentConfig.from_yaml_file(p)


def test_non_mapping_rejected(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        AgentConfig.from_yaml_file(p)


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        AgentConfig.from_yaml_file(tmp_path / "does-not-exist.yaml")


def test_examples_are_valid_yaml_top_level_mappings():
    yaml_examples = sorted(EXAMPLES.glob("*.yaml"))
    assert yaml_examples, "examples/ must contain at least one .yaml reference"
    for example in yaml_examples:
        with example.open() as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), f"{example.name} must be a top-level mapping"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "agent.yaml"
    p.write_text(body)
    return p


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "   ",
        "PR-Reviewer",
        "pr reviewer",
        "pr_reviewer",
        "-leading-hyphen",
        "trailing-hyphen-",
        "a" * 31,
    ],
)
def test_name_rejects_non_slug(tmp_path: Path, bad_name: str):
    p = _write(
        tmp_path,
        f'name: "{bad_name}"\ndescription: d\nsystem_prompt_path: ./CLAUDE.md\n',
    )
    with pytest.raises(ValidationError):
        AgentConfig.from_yaml_file(p)


@pytest.mark.parametrize(
    "bad_env",
    ["lowercase", "has space", "1LEADING_DIGIT", "dash-name", ""],
)
def test_env_rejects_invalid_shell_var_names(tmp_path: Path, bad_env: str):
    p = _write(
        tmp_path,
        'name: ok\ndescription: d\nsystem_prompt_path: ./CLAUDE.md\n'
        f'env:\n  - "{bad_env}"\n',
    )
    with pytest.raises(ValidationError):
        AgentConfig.from_yaml_file(p)


def test_workdir_must_be_absolute(tmp_path: Path):
    p = _write(
        tmp_path,
        "name: ok\ndescription: d\nsystem_prompt_path: ./CLAUDE.md\nworkdir: workspace/foo\n",
    )
    with pytest.raises(ValidationError, match="absolute"):
        AgentConfig.from_yaml_file(p)
