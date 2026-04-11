"""Unit tests for :class:`mars_control.fly.client.FlyClient`.

Uses :class:`httpx.MockTransport` to intercept every request so the
tests never touch the real Fly.io API. Covers the 5 methods the epic
requires (``create_app``, ``create_machine``, ``set_secrets``,
``destroy_machine``, ``list_machines``) plus the incidental helpers
(``delete_app``, ``get_machine``) and the :class:`FlyApiError` error
surface.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from mars_control.fly.client import DEFAULT_BASE_URL, FlyApiError, FlyClient

TOKEN = "fly-test-token"
APP = "mars-pr-reviewer"
MACHINE_ID = "5683d9df4e9789"


def _mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=DEFAULT_BASE_URL,
        timeout=5.0,
    )


def _run(coro) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        FlyClient(api_token="")


def test_injected_client_inherits_auth_header():
    """Passing a pre-built client (tests) should still stamp the
    bearer header so the same client works for downstream test calls."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly._get("/v1/ping")

    _run(_go())
    assert seen["auth"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# create_app + delete_app
# ---------------------------------------------------------------------------


def test_create_app_posts_app_name_and_org():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "app-id-1", "name": APP})

    async def _go() -> dict:
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            return await fly.create_app(APP, org_slug="mars-prod")

    result = _run(_go())
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/apps"
    assert captured["body"] == {"app_name": APP, "org_slug": "mars-prod"}
    assert result == {"id": "app-id-1", "name": APP}


def test_create_app_default_org_is_personal():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201)

    async def _go() -> None:
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.create_app(APP)

    _run(_go())
    assert captured["body"]["org_slug"] == "personal"


def test_delete_app_sends_force_query_by_default():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        return httpx.Response(202)

    async def _go() -> None:
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.delete_app(APP)

    _run(_go())
    assert captured["method"] == "DELETE"
    assert captured["path"] == f"/v1/apps/{APP}"
    assert captured["query"] == {"force": "true"}


def test_delete_app_without_force_omits_query():
    captured: dict[str, Any] = {}

    def handler(request):
        captured["query"] = dict(request.url.params)
        return httpx.Response(202)

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.delete_app(APP, force=False)

    _run(_go())
    assert "force" not in captured["query"]


# ---------------------------------------------------------------------------
# create_machine
# ---------------------------------------------------------------------------


def test_create_machine_posts_config_with_image_and_env():
    captured: dict[str, Any] = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": MACHINE_ID,
                "config": {"image": "ghcr.io/mars/runtime:main"},
            },
        )

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            return await fly.create_machine(
                APP,
                image="ghcr.io/mars/runtime:main",
                env={"CLAUDE_CODE_OAUTH_TOKEN": "secret", "MARS_EVENT_SECRET": "sss"},
                region="iad",
                name="pr-reviewer-machine",
            )

    result = _run(_go())
    assert captured["method"] == "POST"
    assert captured["path"] == f"/v1/apps/{APP}/machines"
    body = captured["body"]
    assert body["region"] == "iad"
    assert body["name"] == "pr-reviewer-machine"
    assert body["config"]["image"] == "ghcr.io/mars/runtime:main"
    assert body["config"]["env"] == {
        "CLAUDE_CODE_OAUTH_TOKEN": "secret",
        "MARS_EVENT_SECRET": "sss",
    }
    assert result["id"] == MACHINE_ID


def test_create_machine_merges_extra_config():
    captured: dict[str, Any] = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": MACHINE_ID})

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.create_machine(
                APP,
                image="ghcr.io/mars/runtime:main",
                extra_config={
                    "services": [{"internal_port": 8080, "protocol": "tcp"}],
                    "restart": {"policy": "on-failure", "max_retries": 3},
                },
            )

    _run(_go())
    config = captured["body"]["config"]
    assert config["image"] == "ghcr.io/mars/runtime:main"
    assert config["services"][0]["internal_port"] == 8080
    assert config["restart"]["policy"] == "on-failure"


def test_create_machine_without_env_omits_env_key():
    captured: dict[str, Any] = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": MACHINE_ID})

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.create_machine(APP, image="ghcr.io/mars/runtime:main")

    _run(_go())
    assert "env" not in captured["body"]["config"]


# ---------------------------------------------------------------------------
# list_machines
# ---------------------------------------------------------------------------


def test_list_machines_returns_list():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == f"/v1/apps/{APP}/machines"
        return httpx.Response(
            200,
            json=[
                {"id": "m1", "state": "started"},
                {"id": "m2", "state": "stopped"},
            ],
        )

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            return await fly.list_machines(APP)

    machines = _run(_go())
    assert [m["id"] for m in machines] == ["m1", "m2"]


def test_list_machines_returns_empty_on_404_unknown_app():
    def handler(request):
        return httpx.Response(404, text="app not found")

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            return await fly.list_machines(APP)

    assert _run(_go()) == []


def test_list_machines_raises_on_other_errors():
    def handler(request):
        return httpx.Response(500, text="boom")

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.list_machines(APP)

    with pytest.raises(FlyApiError) as exc_info:
        _run(_go())
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# destroy_machine
# ---------------------------------------------------------------------------


def test_destroy_machine_sends_force_query():
    captured: dict[str, Any] = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        return httpx.Response(200)

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.destroy_machine(APP, MACHINE_ID)

    _run(_go())
    assert captured["method"] == "DELETE"
    assert captured["path"] == f"/v1/apps/{APP}/machines/{MACHINE_ID}"
    assert captured["query"] == {"force": "true"}


def test_destroy_machine_without_force_omits_query():
    captured: dict[str, Any] = {}

    def handler(request):
        captured["query"] = dict(request.url.params)
        return httpx.Response(200)

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.destroy_machine(APP, MACHINE_ID, force=False)

    _run(_go())
    assert "force" not in captured["query"]


# ---------------------------------------------------------------------------
# set_secrets (GET + POST merge)
# ---------------------------------------------------------------------------


def test_set_secrets_merges_into_existing_env():
    calls: list[tuple[str, str, dict]] = []

    def handler(request):
        method = request.method
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        calls.append((method, path, body))
        if method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": MACHINE_ID,
                    "config": {
                        "image": "ghcr.io/mars/runtime:main",
                        "env": {
                            "EXISTING_KEY": "keep-me",
                            "CLAUDE_CODE_OAUTH_TOKEN": "old",
                        },
                    },
                },
            )
        return httpx.Response(200, json={"ok": True})

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            return await fly.set_secrets(
                APP,
                MACHINE_ID,
                {"CLAUDE_CODE_OAUTH_TOKEN": "new", "MARS_EVENT_SECRET": "xyz"},
            )

    _run(_go())
    # First a GET to fetch state, then a POST to update
    assert calls[0][0] == "GET"
    assert calls[0][1] == f"/v1/apps/{APP}/machines/{MACHINE_ID}"
    assert calls[1][0] == "POST"
    assert calls[1][1] == f"/v1/apps/{APP}/machines/{MACHINE_ID}"
    updated_env = calls[1][2]["config"]["env"]
    assert updated_env == {
        "EXISTING_KEY": "keep-me",  # preserved
        "CLAUDE_CODE_OAUTH_TOKEN": "new",  # overwritten
        "MARS_EVENT_SECRET": "xyz",  # added
    }


def test_set_secrets_on_machine_without_env_creates_it():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": MACHINE_ID, "config": {"image": "x"}},
            )
        body = json.loads(request.content)
        # The POST body must include the merged env even though the
        # machine started without one.
        assert body["config"]["env"] == {"MARS_EVENT_SECRET": "abc"}
        return httpx.Response(200)

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.set_secrets(APP, MACHINE_ID, {"MARS_EVENT_SECRET": "abc"})

    _run(_go())


def test_set_secrets_empty_mapping_rejected():
    def handler(request):
        return httpx.Response(200, json={})

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.set_secrets(APP, MACHINE_ID, {})

    with pytest.raises(ValueError):
        _run(_go())


# ---------------------------------------------------------------------------
# FlyApiError surface
# ---------------------------------------------------------------------------


def test_non_2xx_response_raises_fly_api_error():
    def handler(request):
        return httpx.Response(401, text="unauthorized — bad token")

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.create_app(APP)

    with pytest.raises(FlyApiError) as exc_info:
        _run(_go())
    err = exc_info.value
    assert err.status_code == 401
    assert err.method == "POST"
    assert err.path == "/v1/apps"
    assert "unauthorized" in err.response_text
    assert "401" in str(err)


def test_fly_api_error_truncates_huge_response_bodies():
    def handler(request):
        return httpx.Response(500, text="x" * 5000)

    async def _go():
        async with FlyClient(
            api_token=TOKEN, client=_mock_transport(handler)
        ) as fly:
            await fly.create_app(APP)

    with pytest.raises(FlyApiError) as exc_info:
        _run(_go())
    # Response text truncated to 500 chars to keep log lines sane
    assert len(exc_info.value.response_text) == 500
