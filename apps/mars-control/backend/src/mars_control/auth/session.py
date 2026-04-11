"""JWT session cookie issuance + verification.

Keeping the session-cookie layer separate from :mod:`auth.magic_link`
lets us use different secrets for the two channels (so a leaked
magic-link secret does not compromise live sessions) and different
TTLs (magic links live 15 min, sessions live days).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import jwt

__all__ = [
    "DEFAULT_SESSION_COOKIE_NAME",
    "DEFAULT_SESSION_TTL_SECONDS",
    "SessionCookieService",
    "SessionError",
    "SessionUser",
]

DEFAULT_SESSION_COOKIE_NAME = "mars_session"
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_SESSION_AUDIENCE = "mars-control:session"


class SessionError(ValueError):
    """Raised on invalid / expired session cookies."""


@dataclass
class SessionUser:
    """The claims of a live session cookie."""

    email: str
    issued_at: datetime
    expires_at: datetime


class SessionCookieService:
    """Mint and verify JWT session cookies.

    Args:
        secret: HMAC signing secret. Distinct from the magic-link
            secret.
        ttl_seconds: Session lifetime in seconds. Default 7 days.
        cookie_name: Name of the HTTP cookie. Default ``mars_session``.
        clock: Optional callable returning current UNIX time.
    """

    def __init__(
        self,
        *,
        secret: str,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
        cookie_secure: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("SessionCookieService requires a non-empty secret")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self._secret = secret
        self._ttl = ttl_seconds
        self._cookie_name = cookie_name
        self._cookie_secure = cookie_secure
        self._clock = clock or time.time

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def issue(self, email: str) -> str:
        """Mint a session JWT. Returns the encoded token string.

        Emails are normalized (lower + stripped) so different casings
        of the same email share one session.
        """
        normalized = email.strip().lower()
        now = int(self._clock())
        claims: dict[str, Any] = {
            "sub": normalized,
            "email": normalized,
            "iat": now,
            "exp": now + self._ttl,
            "aud": _SESSION_AUDIENCE,
        }
        return jwt.encode(claims, self._secret, algorithm="HS256")

    def verify(self, token: str) -> SessionUser:
        """Verify a session cookie string. Raises :class:`SessionError`
        on invalid / expired / wrong-audience tokens.
        """
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=_SESSION_AUDIENCE,
                options={"require": ["exp", "iat", "sub", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise SessionError("session expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise SessionError("session audience mismatch") from exc
        except jwt.InvalidTokenError as exc:
            raise SessionError(f"invalid session cookie: {exc}") from exc
        return SessionUser(
            email=claims["email"],
            issued_at=datetime.fromtimestamp(claims["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        )

    # ------------------------------------------------------------------
    # FastAPI integration helpers
    # ------------------------------------------------------------------

    def build_set_cookie_kwargs(self, token: str) -> dict[str, Any]:
        """Return the kwargs passed to ``Response.set_cookie`` for the
        production cookie shape:

        * ``httponly=True``  — JS cannot read it (XSS defense).
        * ``secure``         — True by default; tests pass ``cookie_secure=False``
          to the constructor so TestClient's http:// transport can
          forward the cookie.
        * ``samesite=lax``   — protects against cross-site form POSTs
          while still allowing magic-link GET redirects.
        * ``max_age=TTL``    — the browser expires the cookie even if
          the JWT's ``exp`` claim is still valid.
        """
        return {
            "key": self._cookie_name,
            "value": token,
            "max_age": self._ttl,
            "httponly": True,
            "secure": self._cookie_secure,
            "samesite": "lax",
            "path": "/",
        }

    def build_clear_cookie_kwargs(self) -> dict[str, Any]:
        return {
            "key": self._cookie_name,
            "value": "",
            "max_age": 0,
            "httponly": True,
            "secure": self._cookie_secure,
            "samesite": "lax",
            "path": "/",
        }
