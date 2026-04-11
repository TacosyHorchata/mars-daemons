"""Story 8.2 — template discovery unit tests.

Covers both the module-level ``discover_templates`` / ``load_template``
helpers and the ``GET /templates`` endpoint that renders the card
grid on the dashboard.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mars_control.api.routes import create_control_app
from mars_control.auth.session import SessionCookieService
from mars_control.sse.stream import SSEEventSink
from mars_control.store.events import EventStore
from mars_control.templates import (
    DEFAULT_TEMPLATE_DIR,
    MissingPromptError,
    TemplateDirMissingError,
    TemplateSummary,
    discover_templates,
    load_template,
)

VALID_YAML = """\
name: demo-agent
description: A test agent for unit tests
runtime: claude-code
system_prompt_path: /workspace/demo-agent/demo.prompt.md
workdir: /workspace/demo-agent
mcps:
  - whatsapp
tools:
  - Read
env:
  - DEMO_API_KEY
"""

VALID_PROMPT = """\
# Demo Agent

Eres un agente de prueba. Habla en español.

## Sección

Más contenido aquí.
"""

INVALID_YAML = "this: is: not: valid: yaml: because: : :\n"
BROKEN_SCHEMA_YAML = """\
name: has spaces which are forbidden
description: broken
runtime: claude-code
system_prompt_path: relative/path
workdir: /workspace/broken
"""


SESSION_SECRET = "test-session-secret-at-least-32-bytes-long-ok"
TEST_EMAIL = "tester@example.com"


@pytest.fixture
def tmp_template_dir(tmp_path: Path) -> Path:
    """Build a tmp templates/ directory with one valid template."""
    d = tmp_path / "templates"
    d.mkdir()
    (d / "demo-agent.yaml").write_text(VALID_YAML, encoding="utf-8")
    (d / "demo-agent.prompt.md").write_text(VALID_PROMPT, encoding="utf-8")
    return d


def _make_client(template_dir: Path) -> TestClient:
    session_service = SessionCookieService(
        secret=SESSION_SECRET, cookie_secure=False
    )
    store = EventStore(":memory:")
    store.init()
    app = create_control_app(
        store=store,
        event_secret="x",
        sink=SSEEventSink(),
        session_service=session_service,
        template_dir=template_dir,
    )
    client = TestClient(app)
    client.cookies.set(
        session_service.cookie_name, session_service.issue(TEST_EMAIL)
    )
    return client


# ---------------------------------------------------------------------------
# discover_templates unit-level
# ---------------------------------------------------------------------------


def test_discover_templates_returns_valid_entries(tmp_template_dir: Path) -> None:
    results = discover_templates(tmp_template_dir)
    assert len(results) == 1
    summary = results[0]
    assert summary.name == "demo-agent"
    assert summary.runtime == "claude-code"
    assert "whatsapp" in summary.mcps
    # Prompt preview should be the first non-header, non-empty line.
    assert "Eres un agente de prueba" in summary.system_prompt_preview


def test_discover_templates_raises_on_missing_dir(tmp_path: Path) -> None:
    """Codex review — previously this returned an empty list which
    silently hid a misconfigured MARS_TEMPLATE_DIR. Now it raises
    TemplateDirMissingError so the failure is loud."""
    with pytest.raises(TemplateDirMissingError) as exc_info:
        discover_templates(tmp_path / "nope")
    assert "MARS_TEMPLATE_DIR" in str(exc_info.value)


def test_discover_templates_skips_broken_yaml(tmp_template_dir: Path) -> None:
    (tmp_template_dir / "broken.yaml").write_text(INVALID_YAML, encoding="utf-8")
    (tmp_template_dir / "bad-schema.yaml").write_text(
        BROKEN_SCHEMA_YAML, encoding="utf-8"
    )
    results = discover_templates(tmp_template_dir)
    # The valid demo-agent survives; the two broken ones are dropped.
    assert [r.name for r in results] == ["demo-agent"]


def test_discover_templates_skips_template_with_missing_prompt(
    tmp_path: Path,
) -> None:
    """Codex review — a template YAML with no sibling .prompt.md
    cannot be deployed, so we refuse to advertise it on the
    dashboard. Logged as WARNING and skipped."""
    d = tmp_path / "templates"
    d.mkdir()
    (d / "no-prompt.yaml").write_text(
        VALID_YAML.replace("demo-agent", "no-prompt").replace(
            "demo.prompt.md", "no-prompt.prompt.md"
        ),
        encoding="utf-8",
    )
    # Note: no sibling no-prompt.prompt.md file created.
    assert discover_templates(d) == []


def test_load_template_raises_missing_prompt_error(tmp_path: Path) -> None:
    """Explicit unit-level: load_template() surfaces MissingPromptError
    directly so callers can distinguish schema-invalid from
    prompt-missing templates."""
    d = tmp_path / "templates"
    d.mkdir()
    yaml_path = d / "orphan.yaml"
    yaml_path.write_text(
        VALID_YAML.replace("demo-agent", "orphan").replace(
            "demo.prompt.md", "orphan.prompt.md"
        ),
        encoding="utf-8",
    )
    with pytest.raises(MissingPromptError):
        load_template(yaml_path)


def test_discover_templates_propagates_unexpected_errors(tmp_path: Path) -> None:
    """Codex review — unexpected exceptions (programmer bugs, OS
    errors) should NOT be silently swallowed. Only the expected
    parse/validation/missing-prompt errors are caught. We verify
    this by making the directory's globbed file unreadable at the
    OS level and asserting that propagates."""
    import os
    import stat

    d = tmp_path / "templates"
    d.mkdir()
    yaml_path = d / "locked.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    (d / "locked.prompt.md").write_text(VALID_PROMPT, encoding="utf-8")
    # Strip read permissions. On POSIX this makes the file unreadable
    # for non-root users, which raises PermissionError from the open()
    # inside AgentConfig.from_yaml_file — NOT a YAMLError or
    # ValidationError, so it must propagate.
    os.chmod(yaml_path, 0)
    try:
        # Skip on platforms where root ignores chmod (CI runners, etc).
        if os.access(yaml_path, os.R_OK):
            pytest.skip("file is still readable — running as root?")
        with pytest.raises(PermissionError):
            discover_templates(d)
    finally:
        os.chmod(yaml_path, stat.S_IRUSR | stat.S_IWUSR)


def test_discover_templates_is_sorted_by_name(tmp_path: Path) -> None:
    d = tmp_path / "templates"
    d.mkdir()
    for name in ("zeta", "alpha", "mike"):
        (d / f"{name}.yaml").write_text(
            VALID_YAML.replace("demo-agent", name).replace(
                "demo.prompt.md", f"{name}.prompt.md"
            ),
            encoding="utf-8",
        )
        (d / f"{name}.prompt.md").write_text(VALID_PROMPT, encoding="utf-8")
    results = discover_templates(d)
    assert [r.name for r in results] == ["alpha", "mike", "zeta"]


def test_load_template_single_file(tmp_template_dir: Path) -> None:
    yaml_path = tmp_template_dir / "demo-agent.yaml"
    summary = load_template(yaml_path)
    assert summary.name == "demo-agent"
    assert summary.description.startswith("A test agent")


def test_template_summary_to_dict_shape(tmp_template_dir: Path) -> None:
    summary = load_template(tmp_template_dir / "demo-agent.yaml")
    d = summary.to_dict()
    assert set(d.keys()) == {
        "name",
        "description",
        "runtime",
        "mcps",
        "system_prompt_preview",
    }
    # mcps is a list (JSON-serializable), not a tuple
    assert isinstance(d["mcps"], list)


# ---------------------------------------------------------------------------
# GET /templates endpoint
# ---------------------------------------------------------------------------


def test_list_templates_endpoint_returns_discovered(tmp_template_dir: Path) -> None:
    client = _make_client(tmp_template_dir)
    resp = client.get("/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert "templates" in data
    assert len(data["templates"]) == 1
    assert data["templates"][0]["name"] == "demo-agent"
    assert data["templates"][0]["mcps"] == ["whatsapp"]


def test_list_templates_endpoint_401_without_cookie(tmp_template_dir: Path) -> None:
    client = _make_client(tmp_template_dir)
    client.cookies.clear()
    resp = client.get("/templates")
    assert resp.status_code == 401


def test_list_templates_endpoint_500_when_dir_missing(tmp_path: Path) -> None:
    """Codex review — misconfigured template dir surfaces as 500
    with a helpful detail instead of an empty-list false success."""
    client = _make_client(tmp_path / "nope")
    resp = client.get("/templates")
    assert resp.status_code == 500
    assert "MARS_TEMPLATE_DIR" in resp.json()["detail"]


def test_list_templates_uses_real_tracker_ops_template_by_default() -> None:
    """Integration-flavored — the repo's real templates/ directory
    contains the Story 8.1 tracker-ops-assistant, and the endpoint
    picks it up when no template_dir is passed."""
    session_service = SessionCookieService(
        secret=SESSION_SECRET, cookie_secure=False
    )
    store = EventStore(":memory:")
    store.init()
    app = create_control_app(
        store=store,
        event_secret="x",
        sink=SSEEventSink(),
        session_service=session_service,
    )
    client = TestClient(app)
    client.cookies.set(
        session_service.cookie_name, session_service.issue(TEST_EMAIL)
    )
    resp = client.get("/templates")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()["templates"]]
    assert "tracker-ops-assistant" in names
