"""Unit tests for ``mars deploy``.

The ``FlyClient`` is monkey-patched at import site so we never touch
the network. Exercises:

* env + secret option resolution (CLI flags vs env vars)
* AgentConfig loading + error surface on bad yaml
* Fly app create + "already exists" tolerance
* Machine create with the expected env + extra_config shapes
* Exit codes on missing fly-token / event-secret
* FlyApiError bubbling up as a ClickException with a useful message

The goal is to cover every branch of ``deploy_command`` that can be
exercised without a real Fly.io account.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from mars.deploy import (
    DEFAULT_IMAGE,
    _assemble_env,
    _build_machine_config,
    deploy_command,
)
from mars.fly.client import FlyApiError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VALID_AGENT_YAML = """
name: pr-reviewer
description: Reviews pull requests on the mars-daemons repo.
runtime: claude-code
system_prompt_path: CLAUDE.md
workdir: /workspace/pr-reviewer
env:
  - ANTHROPIC_API_KEY
  - GH_TOKEN
tools:
  - Bash
  - Read
"""


class _FakeFlyClient:
    """Records every call so tests can assert against them."""

    #: Class-level register so the module-import-site monkeypatch works.
    calls: list[tuple[str, tuple, dict]] = []
    #: Preset results / side-effects a test can configure.
    create_app_raises: Exception | None = None
    create_machine_result: dict[str, Any] = {"id": "machine-id-1"}

    def __init__(self, *, api_token: str, base_url: str = "", client=None) -> None:
        assert api_token, "api_token must be set"

    async def __aenter__(self) -> "_FakeFlyClient":
        return self

    async def __aexit__(self, *a) -> None:
        pass

    async def create_app(self, name: str, *, org_slug: str = "personal") -> dict:
        _FakeFlyClient.calls.append(("create_app", (name,), {"org_slug": org_slug}))
        if _FakeFlyClient.create_app_raises is not None:
            raise _FakeFlyClient.create_app_raises
        return {"name": name}

    async def create_machine(
        self,
        app_name: str,
        *,
        image: str,
        env: dict[str, str] | None = None,
        region: str | None = None,
        name: str | None = None,
        extra_config: dict[str, Any] | None = None,
    ) -> dict:
        _FakeFlyClient.calls.append(
            (
                "create_machine",
                (app_name,),
                {
                    "image": image,
                    "env": dict(env or {}),
                    "region": region,
                    "name": name,
                    "extra_config": dict(extra_config or {}),
                },
            )
        )
        return dict(_FakeFlyClient.create_machine_result)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeFlyClient.calls = []
    _FakeFlyClient.create_app_raises = None
    _FakeFlyClient.create_machine_result = {"id": "machine-id-1"}


@pytest.fixture
def patch_fly(monkeypatch):
    """Swap the FlyClient class inside mars.deploy for the fake."""
    import mars.deploy as deploy_mod

    monkeypatch.setattr(deploy_mod, "FlyClient", _FakeFlyClient)
    return _FakeFlyClient


@pytest.fixture
def agent_yaml_file(tmp_path: Path) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(VALID_AGENT_YAML.strip() + "\n", encoding="utf-8")
    return path


def _runner(env: dict[str, str] | None = None) -> CliRunner:
    return CliRunner(env=env or {})


# ---------------------------------------------------------------------------
# Helper-function unit tests
# ---------------------------------------------------------------------------


def test_assemble_env_includes_wired_vars_and_forwards_host_secrets():
    env = _assemble_env(
        agent_yaml_text="name: x\n",
        event_secret="sssh",
        control_url="https://mars-control.example",
        declared_env_names=["ANTHROPIC_API_KEY", "NOT_IN_HOST"],
        host_env={"ANTHROPIC_API_KEY": "sk-real", "OTHER": "ignored"},
    )
    assert env["MARS_EVENT_SECRET"] == "sssh"
    assert env["MARS_CONTROL_URL"] == "https://mars-control.example"
    assert env["MARS_DEFAULT_AGENT_YAML"] == "name: x\n"
    # Declared + present in host → forwarded
    assert env["ANTHROPIC_API_KEY"] == "sk-real"
    # Declared but absent from host → silently skipped
    assert "NOT_IN_HOST" not in env
    # Not declared → not forwarded
    assert "OTHER" not in env


def test_assemble_env_empty_control_url_not_included():
    env = _assemble_env(
        agent_yaml_text="x",
        event_secret="s",
        control_url="",
        declared_env_names=[],
        host_env={},
    )
    assert "MARS_CONTROL_URL" not in env


def test_build_machine_config_shape():
    cfg = _build_machine_config(
        image="ghcr.io/mars/runtime:main",
        env={"A": "1"},
        supervisor_port=8080,
    )
    assert cfg["services"][0]["internal_port"] == 8080
    assert cfg["services"][0]["protocol"] == "tcp"
    ports = cfg["services"][0]["ports"]
    assert any(p["port"] == 443 and "tls" in p["handlers"] for p in ports)
    assert any(p["port"] == 80 for p in ports)
    assert cfg["restart"] == {"policy": "on-failure", "max_retries": 3}
    assert cfg["guest"]["cpus"] == 1
    assert cfg["guest"]["memory_mb"] == 1024


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_deploy_happy_path_creates_app_and_machine(
    patch_fly, agent_yaml_file
):
    runner = _runner(
        {
            "FLY_API_TOKEN": "fly-test-token",
            "MARS_EVENT_SECRET": "event-secret-x",
            "MARS_CONTROL_URL": "https://mars-control.example",
            "ANTHROPIC_API_KEY": "sk-host",
        }
    )
    result = runner.invoke(
        deploy_command,
        [str(agent_yaml_file)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    calls = {c[0]: c for c in patch_fly.calls}
    assert "create_app" in calls
    app_call = calls["create_app"]
    assert app_call[1] == ("mars-pr-reviewer",)
    assert app_call[2] == {"org_slug": "personal"}

    assert "create_machine" in calls
    machine_call = calls["create_machine"]
    assert machine_call[1] == ("mars-pr-reviewer",)
    kwargs = machine_call[2]
    assert kwargs["image"] == DEFAULT_IMAGE
    assert kwargs["region"] == "iad"
    assert kwargs["name"] == "pr-reviewer-supervisor"
    env = kwargs["env"]
    assert env["MARS_EVENT_SECRET"] == "event-secret-x"
    assert env["MARS_CONTROL_URL"] == "https://mars-control.example"
    assert "MARS_DEFAULT_AGENT_YAML" in env
    assert env["ANTHROPIC_API_KEY"] == "sk-host"
    # GH_TOKEN was declared in agent.yaml but not set in the runner env — skip
    assert "GH_TOKEN" not in env

    # Machine config shape checks
    extra = kwargs["extra_config"]
    assert "services" in extra
    assert extra["services"][0]["internal_port"] == 8080
    assert extra["guest"]["memory_mb"] == 1024

    # Output mentions the app + supervisor health URL
    assert "mars-pr-reviewer" in result.output
    assert "https://mars-pr-reviewer.fly.dev/health" in result.output


def test_deploy_cli_flags_override_env_vars(patch_fly, agent_yaml_file):
    runner = _runner(
        {
            "FLY_API_TOKEN": "env-token",
            "MARS_EVENT_SECRET": "env-secret",
        }
    )
    result = runner.invoke(
        deploy_command,
        [
            str(agent_yaml_file),
            "--fly-token",
            "flag-token",
            "--event-secret",
            "flag-secret",
            "--control-url",
            "https://flag-control",
            "--app",
            "mars-custom-app",
            "--region",
            "ord",
            "--image",
            "ghcr.io/mars/runtime:pr-123",
            "--org",
            "mars-prod",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    create_app = next(c for c in patch_fly.calls if c[0] == "create_app")
    assert create_app[1] == ("mars-custom-app",)
    assert create_app[2]["org_slug"] == "mars-prod"

    create_machine = next(c for c in patch_fly.calls if c[0] == "create_machine")
    k = create_machine[2]
    assert k["image"] == "ghcr.io/mars/runtime:pr-123"
    assert k["region"] == "ord"
    assert k["env"]["MARS_EVENT_SECRET"] == "flag-secret"
    assert k["env"]["MARS_CONTROL_URL"] == "https://flag-control"


def test_deploy_tolerates_app_already_exists(patch_fly, agent_yaml_file):
    _FakeFlyClient.create_app_raises = FlyApiError(
        method="POST", path="/v1/apps", status_code=409, response_text="exists"
    )
    runner = _runner(
        {
            "FLY_API_TOKEN": "t",
            "MARS_EVENT_SECRET": "s",
        }
    )
    result = runner.invoke(
        deploy_command, [str(agent_yaml_file)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "already exists" in result.output
    # Still created the machine
    assert any(c[0] == "create_machine" for c in patch_fly.calls)


def test_deploy_tolerates_422_already_exists(patch_fly, agent_yaml_file):
    _FakeFlyClient.create_app_raises = FlyApiError(
        method="POST", path="/v1/apps", status_code=422, response_text="exists"
    )
    runner = _runner({"FLY_API_TOKEN": "t", "MARS_EVENT_SECRET": "s"})
    result = runner.invoke(
        deploy_command, [str(agent_yaml_file)], catch_exceptions=False
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_deploy_missing_fly_token_errors(patch_fly, agent_yaml_file):
    runner = _runner({"MARS_EVENT_SECRET": "s"})  # no FLY_API_TOKEN
    result = runner.invoke(deploy_command, [str(agent_yaml_file)])
    assert result.exit_code != 0
    assert "Fly API token" in result.output


def test_deploy_missing_event_secret_errors(patch_fly, agent_yaml_file):
    runner = _runner({"FLY_API_TOKEN": "t"})
    result = runner.invoke(deploy_command, [str(agent_yaml_file)])
    assert result.exit_code != 0
    assert "event secret" in result.output.lower()


def test_deploy_invalid_agent_yaml_errors(patch_fly, tmp_path):
    bad = tmp_path / "agent.yaml"
    bad.write_text("not: a valid: agent yaml\n[broken]", encoding="utf-8")
    runner = _runner({"FLY_API_TOKEN": "t", "MARS_EVENT_SECRET": "s"})
    result = runner.invoke(deploy_command, [str(bad)])
    assert result.exit_code != 0
    assert "agent.yaml" in result.output


def test_deploy_fly_api_error_on_create_app_non_409_bubbles_up(
    patch_fly, agent_yaml_file
):
    _FakeFlyClient.create_app_raises = FlyApiError(
        method="POST", path="/v1/apps", status_code=401, response_text="bad token"
    )
    runner = _runner({"FLY_API_TOKEN": "bad", "MARS_EVENT_SECRET": "s"})
    result = runner.invoke(deploy_command, [str(agent_yaml_file)])
    assert result.exit_code != 0
    assert "Fly API error" in result.output
    assert "401" in result.output
