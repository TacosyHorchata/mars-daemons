"""Magic-link token issuance + verification.

v1 design:

* The control plane signs a short-lived JWT (default 15 min) carrying
  the user's email + a ``jti`` (token id). The JWT is embedded in a
  magic-link URL sent via email.
* When the user clicks the link, the control plane verifies the JWT
  and checks ``jti`` against a consumed-tokens set so each link is
  single-use.
* On successful verification the control plane issues the user's
  session cookie (see :mod:`auth.session`) and the magic link is
  burned.

The consumed-tokens set is **in-memory** for v1 single-host. When we
add multi-node control planes (v2) the set moves to SQLite with a
``consumed_at`` timestamp + a periodic sweep for expired tokens.

The design deliberately avoids a separate "pending magic links"
table — JWT stateless tokens + a tiny consumed-set gives us all the
single-use semantics we need with one-thousandth the schema churn.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import jwt

__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "MagicLinkError",
    "MagicLinkService",
    "MagicLinkToken",
]

DEFAULT_TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes
_MAGIC_LINK_AUDIENCE = "mars-control:magic-link"


class MagicLinkError(ValueError):
    """Raised on invalid / expired / already-consumed magic-link tokens."""


@dataclass
class MagicLinkToken:
    """The verified claims of a magic-link token."""

    email: str
    jti: str
    issued_at: datetime
    expires_at: datetime


class MagicLinkService:
    """Issue and verify magic-link JWTs.

    Args:
        secret: HMAC signing secret. Must be kept on the control
            plane only — leaking it lets anyone mint auth tokens.
        ttl_seconds: Token lifetime in seconds. Default 15 min.
        clock: Optional callable returning the current UNIX time.
            Tests pin this for deterministic expiry checks.
    """

    def __init__(
        self,
        *,
        secret: str,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("MagicLinkService requires a non-empty secret")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self._secret = secret
        self._ttl = ttl_seconds
        self._clock = clock or time.time
        self._consumed: set[str] = set()

    # ------------------------------------------------------------------
    # Issuance
    # ------------------------------------------------------------------

    def issue(self, email: str) -> str:
        """Mint a magic-link JWT for ``email``. Returns the encoded
        token string.

        Emails are lower-cased before claiming so future "same email,
        different case" visits hit the same user.
        """
        normalized = email.strip().lower()
        if not normalized or "@" not in normalized:
            raise ValueError(f"invalid email {email!r}")
        now = int(self._clock())
        jti = secrets.token_urlsafe(24)
        claims: dict[str, Any] = {
            "sub": normalized,
            "email": normalized,
            "iat": now,
            "exp": now + self._ttl,
            "aud": _MAGIC_LINK_AUDIENCE,
            "jti": jti,
        }
        return jwt.encode(claims, self._secret, algorithm="HS256")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, token: str) -> MagicLinkToken:
        """Verify a magic-link JWT.

        Raises :class:`MagicLinkError` if the token is:

        * Signature invalid
        * Expired
        * Audience mismatch (reused session cookie for example)
        * Already consumed via :meth:`consume`
        """
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=_MAGIC_LINK_AUDIENCE,
                options={"require": ["exp", "iat", "sub", "jti", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise MagicLinkError("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise MagicLinkError("token audience mismatch") from exc
        except jwt.InvalidTokenError as exc:
            raise MagicLinkError(f"invalid token: {exc}") from exc

        jti = claims["jti"]
        if jti in self._consumed:
            raise MagicLinkError("token already consumed")

        return MagicLinkToken(
            email=claims["email"],
            jti=jti,
            issued_at=datetime.fromtimestamp(claims["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        )

    def consume(self, token: MagicLinkToken) -> None:
        """Burn a token's ``jti`` so it cannot be re-used.

        Idempotent — consuming an already-consumed token is a no-op
        rather than an error (the expected call pattern is
        verify → consume → issue-session, and a retry mid-flow shouldn't
        block the user).
        """
        self._consumed.add(token.jti)

    # ------------------------------------------------------------------
    # Introspection (tests + observability)
    # ------------------------------------------------------------------

    @property
    def consumed_count(self) -> int:
        return len(self._consumed)
