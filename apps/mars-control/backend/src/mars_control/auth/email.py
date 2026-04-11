"""Email transport for magic-link auth.

Defines a narrow :class:`EmailSender` protocol — any async callable
that can ``send(to, subject, body)`` — and ships two implementations:

* :class:`ResendEmailSender` — POSTs to the Resend REST API.
* :class:`InMemoryEmailSender` — records every call for tests.

Mars never shells out to ``flyctl``-style tooling here; the whole
thing is pure ``httpx`` + Pydantic so tests can MockTransport the
real network path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

__all__ = [
    "EmailSendError",
    "EmailSender",
    "InMemoryEmailSender",
    "ResendEmailSender",
    "SentMail",
]


class EmailSendError(RuntimeError):
    """Raised when an email provider returns a non-2xx response."""


class EmailSender(Protocol):
    """Shape every Mars email transport implements."""

    async def send(
        self, *, to: str, subject: str, body_text: str
    ) -> None: ...


class ResendEmailSender:
    """Production sender that POSTs to Resend's ``/emails`` endpoint.

    Args:
        api_key: Resend API key (``re_*``).
        from_address: Verified sender address. Must have DKIM set up.
        base_url: API root. Defaults to the production Resend URL.
        client: Optional pre-built :class:`httpx.AsyncClient`. Tests
            pass a MockTransport-backed client to exercise the exact
            request shape without touching the network.
    """

    DEFAULT_BASE_URL = "https://api.resend.com"

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ResendEmailSender requires a non-empty api_key")
        if not from_address:
            raise ValueError("ResendEmailSender requires a non-empty from_address")
        self._api_key = api_key
        self._from = from_address
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(
        self, *, to: str, subject: str, body_text: str
    ) -> None:
        payload = {
            "from": self._from,
            "to": [to],
            "subject": subject,
            "text": body_text,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        client = self._client or httpx.AsyncClient(timeout=10.0)
        owns_client = self._client is None
        try:
            try:
                resp = await client.post(
                    f"{self._base_url}/emails", json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                raise EmailSendError(
                    f"Resend request failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise EmailSendError(
                    f"Resend returned {resp.status_code}: {resp.text[:500]}"
                )
        finally:
            if owns_client:
                await client.aclose()


@dataclass
class SentMail:
    """Record of a single call to :meth:`InMemoryEmailSender.send`."""

    to: str
    subject: str
    body_text: str


@dataclass
class InMemoryEmailSender:
    """Test-only sender that records every call instead of sending.

    Fulfills the :class:`EmailSender` protocol so tests can inject
    it into ``create_control_app`` and then assert against
    :attr:`outbox`.
    """

    outbox: list[SentMail] = field(default_factory=list)

    async def send(
        self, *, to: str, subject: str, body_text: str
    ) -> None:
        self.outbox.append(
            SentMail(to=to, subject=subject, body_text=body_text)
        )
