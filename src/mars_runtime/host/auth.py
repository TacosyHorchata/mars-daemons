"""Simple bearer-token auth for the standalone host."""

from __future__ import annotations

import os
import secrets
from typing import Any

from ..core.exceptions import AuthenticationError
from ..core.tools import AuthContext


class StaticBearerAuthProvider:
    """Minimal auth provider for standalone deployments.

    The token is checked against ``MARS_AUTH_TOKEN`` or an explicit constructor
    argument. Org/user scoping comes from headers with safe defaults so the core
    can stay multi-tenant without bringing in JWT/Firebase/Mongo dependencies.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        default_org_id: str | None = None,
        default_user_id: str | None = None,
    ) -> None:
        self._token = token if token is not None else os.getenv("MARS_AUTH_TOKEN", "")
        self._default_org_id = default_org_id or os.getenv("MARS_DEFAULT_ORG_ID", "default")
        self._default_user_id = default_user_id or os.getenv("MARS_DEFAULT_USER_ID", "anonymous")

    async def authenticate(self, request: Any) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise AuthenticationError("Authorization token is missing or invalid", status_code=401)

        bearer_token = authorization.removeprefix("Bearer ").strip()
        if not bearer_token:
            raise AuthenticationError("Authorization token is missing or invalid", status_code=401)

        if not self._token:
            raise AuthenticationError(
                "MARS_AUTH_TOKEN is not configured",
                status_code=503,
            )

        if not secrets.compare_digest(bearer_token, self._token):
            raise AuthenticationError("Invalid token", status_code=401)

        org_id = request.headers.get("X-Mars-Org-Id", self._default_org_id).strip() or self._default_org_id
        user_id = request.headers.get("X-Mars-User-Id", self._default_user_id).strip() or self._default_user_id

        return AuthContext(
            org_id=org_id,
            user_id=user_id,
            bearer_token=bearer_token,
            metadata={"auth_type": "static_bearer"},
        )
