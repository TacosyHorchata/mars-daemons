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


def test_discover_templates_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    assert discover_templates(tmp_path / "nope") == []


def test_discover_templates_skips_broken_yaml(tmp_template_dir: Path) -> None:
    (tmp_template_dir / "broken.yaml").write_text(INVALID_YAML, encoding="utf-8")
    (tmp_template_dir / "bad-schema.yaml").write_text(
        BROKEN_SCHEMA_YAML, encoding="utf-8"
    )
    results = discover_templates(tmp_template_dir)
    # The valid demo-agent survives; the two broken ones are dropped.
    assert [r.name for r in results] == ["demo-agent"]


def test_discover_templates_is_sorted_by_name(tmp_path: Path) -> None:
    d = tmp_path / "templates"
    d.mkdir()
    for name in ("zeta", "alpha", "mike"):
        (d / f"{name}.yaml").write_text(
            VALID_YAML.replace("demo-agent", name).replace("demo.prompt.md", f"{name}.prompt.md"),
            encoding="utf-8",
        )
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


def test_list_templates_endpoint_empty_when_dir_missing(tmp_path: Path) -> None:
    client = _make_client(tmp_path / "nope")
    resp = client.get("/templates")
    assert resp.status_code == 200
    assert resp.json() == {"templates": []}


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
