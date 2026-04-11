"""Story 8.1 test — the Tracker Ops Assistant template must be a
valid ``AgentConfig`` that passes ``from_yaml_file`` as-is.

The template lives under ``apps/mars-control/templates/`` and is the
pre-baked agent definition that the onboarding wizard (Story 8.3/8.4)
uses to deploy a daemon for Maat. If this file ever fails validation,
Maat's deploy button also fails — so we gate on it in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schema.agent import AgentConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = _REPO_ROOT / "apps" / "mars-control" / "templates"
TEMPLATE_YAML = TEMPLATE_DIR / "tracker-ops-assistant.yaml"
TEMPLATE_PROMPT = TEMPLATE_DIR / "tracker-ops-assistant.prompt.md"


def test_template_yaml_exists_and_is_readable() -> None:
    assert TEMPLATE_YAML.is_file(), f"expected {TEMPLATE_YAML} to exist"
    assert TEMPLATE_PROMPT.is_file(), f"expected {TEMPLATE_PROMPT} to exist"


def test_template_yaml_parses_as_agent_config() -> None:
    config = AgentConfig.from_yaml_file(TEMPLATE_YAML)
    assert config.name == "tracker-ops-assistant"
    assert config.runtime == "claude-code"
    assert config.workdir.startswith("/")
    # System prompt path points inside the workdir so the runtime
    # can resolve it after the template is copied into the machine.
    assert config.system_prompt_path.endswith(".prompt.md")


def test_template_declares_whatsapp_zoho_pilot_mcps() -> None:
    config = AgentConfig.from_yaml_file(TEMPLATE_YAML)
    assert set(config.mcps) == {"whatsapp", "zoho", "pilot"}


def test_template_env_contains_required_secrets() -> None:
    config = AgentConfig.from_yaml_file(TEMPLATE_YAML)
    # These are the secret NAMES the onboarding wizard must collect
    # values for before deploy. Breaking this set is a breaking change
    # to the wizard form; flag it via the test.
    required = {
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "WHATSAPP_SESSION_NAME",
        "ZOHO_CLIENT_ID",
        "ZOHO_CLIENT_SECRET",
        "ZOHO_REFRESH_TOKEN",
        "PILOT_SESSION_NAME",
    }
    assert set(config.env) == required


def test_template_prompt_is_spanish() -> None:
    """Smoke test — the prompt should be written in Spanish since
    Maat is a native Spanish speaker. We look for a few distinctive
    Spanish stop-words that wouldn't appear in an accidentally-English
    draft."""
    content = TEMPLATE_PROMPT.read_text(encoding="utf-8")
    spanish_markers = ["Eres", "español", "Tu trabajo", "herramientas"]
    for marker in spanish_markers:
        assert marker in content, f"expected {marker!r} in Spanish system prompt"


def test_template_prompt_mentions_all_three_mcps() -> None:
    content = TEMPLATE_PROMPT.read_text(encoding="utf-8").lower()
    assert "whatsapp" in content
    assert "zoho" in content
    assert "pilot" in content


def test_template_description_mentions_spanish() -> None:
    """Product constraint — the template's public description must
    advertise that the agent speaks Spanish, since that is the primary
    differentiator from every english-first tool Maat already ignored.
    """
    config = AgentConfig.from_yaml_file(TEMPLATE_YAML)
    assert "español" in config.description.lower()
