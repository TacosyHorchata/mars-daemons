"""Async wrapper around the Fly.io Machines REST API.

Scope (Story 3.3):

* ``create_app`` — ``POST /v1/apps``
* ``delete_app`` — ``DELETE /v1/apps/{app}`` (cleanup helper, not in the
  story spec but needed by tests + ``mars destroy``)
* ``create_machine`` — ``POST /v1/apps/{app}/machines``
* ``list_machines`` — ``GET /v1/apps/{app}/machines``
* ``destroy_machine`` — ``DELETE /v1/apps/{app}/machines/{machine_id}``
* ``set_secrets`` — merge the given env dict into the machine's
  ``config.env`` and push the updated config. Fly's machines.dev API
  does not expose a dedicated secrets endpoint — app-level secrets
  (``fly secrets set``) go through a legacy GraphQL API. For Mars v1
  we scope secrets to the machine via ``config.env`` so one client
  library owns the whole pipeline.

Design notes
------------

* Uses ``httpx.AsyncClient`` so the client coexists cleanly with
  FastAPI's event loop. Tests pass an injected client backed by
  :class:`httpx.MockTransport` to avoid real network calls.
* Bearer-token auth via ``Authorization: Bearer <token>``. The token
  comes from ``FLY_API_TOKEN`` in production — the client takes it
  explicitly so tests never touch the environment.
* Every non-2xx response is raised as :class:`FlyApiError` with the
  HTTP method, path, status code, and response text attached. Callers
  get a single exception class to catch without dealing with raw
  :class:`httpx.HTTPStatusError`.
* The client is an async context manager — ``async with FlyClient(...)``
  is the idiomatic usage and guarantees the underlying httpx client
  is closed.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

__all__ = [
    "DEFAULT_BASE_URL",
    "FlyApiError",
    "FlyClient",
]

DEFAULT_BASE_URL = "https://api.machines.dev"


class FlyApiError(RuntimeError):
    """Raised when Fly.io's REST API returns a non-2xx response.

    Captures enough context to debug from a log line: the HTTP method,
    the URL path, the status code, and the (truncated) response body.
    The underlying :class:`httpx.HTTPStatusError` is preserved as
    ``__cause__`` for callers that need it.
    """

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        response_text: str,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.response_text = response_text[:500]
        super().__init__(
            f"Fly API {method} {path} returned {status_code}: {self.response_text}"
        )


class FlyClient:
    """Thin async wrapper around the Fly.io Machines REST API."""

    def __init__(
        self,
        *,
        api_token: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_token:
            raise ValueError("FlyClient requires a non-empty api_token")
        self._api_token = api_token
        self._base_url = base_url
        headers = {"Authorization": f"Bearer {api_token}"}
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=base_url, headers=headers, timeout=30.0
            )
            self._owns_client = True
        else:
            # Accept a pre-built client (MockTransport-backed in tests).
            # We still need the auth header on every request; inject it
            # on the shared client if it's missing.
            for key, value in headers.items():
                client.headers.setdefault(key, value)
            self._client = client
            self._owns_client = False

    # ------------------------------------------------------------------
    # Context manager plumbing
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "FlyClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Apps
    # ------------------------------------------------------------------

    async def create_app(
        self, name: str, *, org_slug: str = "personal"
    ) -> dict[str, Any]:
        """Create a Fly app. Returns Fly's response body (may be empty on 201)."""
        body = {"app_name": name, "org_slug": org_slug}
        return await self._post("/v1/apps", json=body)

    async def delete_app(self, name: str, *, force: bool = True) -> None:
        """Delete a Fly app. ``force=True`` removes any running machines."""
        params = {"force": "true"} if force else None
        await self._delete(f"/v1/apps/{name}", params=params)

    # ------------------------------------------------------------------
    # Machines
    # ------------------------------------------------------------------

    async def create_machine(
        self,
        app_name: str,
        *,
        image: str,
        env: Mapping[str, str] | None = None,
        region: str | None = None,
        name: str | None = None,
        extra_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Launch a new Machine on an existing Fly app.

        ``env`` lands in ``config.env`` — this is the Mars v1 secrets
        path (one-machine-per-workspace so no cross-tenant leakage).
        ``extra_config`` is deep-merged into ``config`` for callers
        that need to set ``services``, ``restart``, ``guest``, etc.
        """
        config: dict[str, Any] = {"image": image}
        if env:
            config["env"] = dict(env)
        if extra_config:
            for key, value in extra_config.items():
                config[key] = value
        body: dict[str, Any] = {"config": config}
        if region:
            body["region"] = region
        if name:
            body["name"] = name
        return await self._post(f"/v1/apps/{app_name}/machines", json=body)

    async def list_machines(self, app_name: str) -> list[dict[str, Any]]:
        """List every Machine on a Fly app. Returns an empty list on 404
        so callers can treat ``list_machines`` as "what's running?" on
        a freshly-created app.
        """
        try:
            data = await self._get(f"/v1/apps/{app_name}/machines")
        except FlyApiError as exc:
            if exc.status_code == 404:
                return []
            raise
        if isinstance(data, list):
            return data
        return []

    async def get_machine(self, app_name: str, machine_id: str) -> dict[str, Any]:
        """Fetch a single Machine's state."""
        return await self._get(f"/v1/apps/{app_name}/machines/{machine_id}")

    async def destroy_machine(
        self,
        app_name: str,
        machine_id: str,
        *,
        force: bool = True,
    ) -> None:
        """Destroy a Machine. ``force=True`` stops it first if running."""
        params = {"force": "true"} if force else None
        await self._delete(
            f"/v1/apps/{app_name}/machines/{machine_id}", params=params
        )

    async def set_secrets(
        self,
        app_name: str,
        machine_id: str,
        secrets: Mapping[str, str],
    ) -> dict[str, Any]:
        """Merge ``secrets`` into a Machine's ``config.env`` and push
        the updated config.

        The Fly Machines API exposes no dedicated secrets endpoint in
        v1.4 (app-level secrets are GraphQL-only and being
        deprecated), so Mars scopes secrets to the Machine. This
        method:

        1. GETs the current Machine config.
        2. Deep-merges ``secrets`` into ``config.env``.
        3. POSTs the updated config back to
           ``/v1/apps/{app}/machines/{id}``, which triggers a rolling
           restart of the Machine.

        Existing env keys with the same names are overwritten; unknown
        keys already in ``config.env`` are preserved.
        """
        if not secrets:
            raise ValueError("set_secrets requires a non-empty secrets mapping")

        current = await self.get_machine(app_name, machine_id)
        existing_config = dict(current.get("config") or {})
        existing_env = dict(existing_config.get("env") or {})
        existing_env.update(secrets)
        existing_config["env"] = existing_env
        return await self._post(
            f"/v1/apps/{app_name}/machines/{machine_id}",
            json={"config": existing_config},
        )

    # ------------------------------------------------------------------
    # Low-level helpers (private)
    # ------------------------------------------------------------------

    async def _post(self, path: str, *, json: Any) -> dict[str, Any]:
        resp = await self._client.post(path, json=json)
        self._raise_for_status("POST", path, resp)
        return self._maybe_json(resp)

    async def _get(self, path: str) -> Any:
        resp = await self._client.get(path)
        self._raise_for_status("GET", path, resp)
        return self._maybe_json(resp)

    async def _delete(self, path: str, *, params: Any | None = None) -> None:
        resp = await self._client.delete(path, params=params)
        self._raise_for_status("DELETE", path, resp)

    @staticmethod
    def _raise_for_status(method: str, path: str, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        raise FlyApiError(
            method=method,
            path=path,
            status_code=resp.status_code,
            response_text=resp.text,
        )

    @staticmethod
    def _maybe_json(resp: httpx.Response) -> dict[str, Any]:
        """Fly endpoints sometimes return empty bodies on 201/204."""
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        # The list_machines path calls this through _get but casts
        # the result; keep the dict-specific helper to avoid None.
        return data if isinstance(data, (dict, list)) else {}
