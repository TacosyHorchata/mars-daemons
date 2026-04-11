"""Unit tests for Story 4.2 — magic-link backend auth."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mars_control.api.routes import create_control_app
from mars_control.auth.email import (
    EmailSendError,
    InMemoryEmailSender,
    ResendEmailSender,
)
from mars_control.auth.magic_link import (
    DEFAULT_TOKEN_TTL_SECONDS,
    MagicLinkError,
    MagicLinkService,
)
from mars_control.auth.session import (
    DEFAULT_SESSION_COOKIE_NAME,
    SessionCookieService,
    SessionError,
)
from mars_control.sse.stream import SSEEventSink
from mars_control.store.events import EventStore

MAGIC_SECRET = "magic-test-secret-000000000000000000"  # ≥32 bytes
SESSION_SECRET = "session-test-secret-000000000000000000"


# ---------------------------------------------------------------------------
# MagicLinkService — issuance + verification
# ---------------------------------------------------------------------------


def test_magic_link_requires_non_empty_secret():
    with pytest.raises(ValueError):
        MagicLinkService(secret="")


def test_magic_link_rejects_invalid_email():
    svc = MagicLinkService(secret=MAGIC_SECRET)
    with pytest.raises(ValueError):
        svc.issue("not-an-email")
    with pytest.raises(ValueError):
        svc.issue("   ")


def test_magic_link_issue_verify_roundtrip_normalizes_email():
    svc = MagicLinkService(secret=MAGIC_SECRET)
    token = svc.issue("Pedro@Example.COM")
    verified = svc.verify(token)
    assert verified.email == "pedro@example.com"
    assert verified.jti


def test_magic_link_single_use_after_consume():
    svc = MagicLinkService(secret=MAGIC_SECRET)
    token = svc.issue("pedro@example.com")
    verified = svc.verify(token)
    svc.consume(verified)
    with pytest.raises(MagicLinkError, match="already consumed"):
        svc.verify(token)


def test_magic_link_expiry():
    clock_value = {"t": 1_000_000}
    svc = MagicLinkService(
        secret=MAGIC_SECRET,
        ttl_seconds=60,
        clock=lambda: clock_value["t"],
    )
    token = svc.issue("pedro@example.com")
    # Move time past expiry
    clock_value["t"] += 61
    with pytest.raises(MagicLinkError, match="expired"):
        svc.verify(token)


def test_magic_link_invalid_signature_rejected():
    svc_a = MagicLinkService(secret=MAGIC_SECRET)
    svc_b = MagicLinkService(secret="another-secret")
    token = svc_a.issue("pedro@example.com")
    with pytest.raises(MagicLinkError):
        svc_b.verify(token)


def test_magic_link_tampered_token_rejected():
    svc = MagicLinkService(secret=MAGIC_SECRET)
    token = svc.issue("pedro@example.com")
    # Splice the middle of the signature so the tampered bytes
    # definitely do not happen to validate.
    header, payload, signature = token.split(".")
    bad_sig = signature[::-1]  # reverse; vanishingly unlikely to verify
    bad = f"{header}.{payload}.{bad_sig}"
    with pytest.raises(MagicLinkError):
        svc.verify(bad)


def test_magic_link_consume_is_idempotent():
    svc = MagicLinkService(secret=MAGIC_SECRET)
    token = svc.issue("pedro@example.com")
    verified = svc.verify(token)
    svc.consume(verified)
    svc.consume(verified)  # second call is a no-op
    assert svc.consumed_count == 1


def test_magic_link_default_ttl_is_15_minutes():
    assert DEFAULT_TOKEN_TTL_SECONDS == 15 * 60


# ---------------------------------------------------------------------------
# SessionCookieService — JWT issuance + verification
# ---------------------------------------------------------------------------


def test_session_service_issue_verify_roundtrip():
    svc = SessionCookieService(secret=SESSION_SECRET)
    cookie = svc.issue("pedro@example.com")
    user = svc.verify(cookie)
    assert user.email == "pedro@example.com"


def test_session_service_rejects_empty_secret():
    with pytest.raises(ValueError):
        SessionCookieService(secret="")


def test_session_service_audience_mismatch_rejected():
    """A magic-link token should NOT be usable as a session cookie."""
    magic = MagicLinkService(secret=SESSION_SECRET)
    sess = SessionCookieService(secret=SESSION_SECRET)
    token = magic.issue("pedro@example.com")
    with pytest.raises(SessionError, match="audience"):
        sess.verify(token)


def test_session_service_expired_cookie_rejected():
    clock_val = {"t": 2_000_000}
    sess = SessionCookieService(
        secret=SESSION_SECRET,
        ttl_seconds=10,
        clock=lambda: clock_val["t"],
    )
    cookie = sess.issue("pedro@example.com")
    clock_val["t"] += 11
    with pytest.raises(SessionError, match="expired"):
        sess.verify(cookie)


def test_session_cookie_kwargs_are_secure_httponly_lax():
    sess = SessionCookieService(secret=SESSION_SECRET)
    kwargs = sess.build_set_cookie_kwargs("some-token")
    assert kwargs["httponly"] is True
    assert kwargs["secure"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["path"] == "/"
    assert kwargs["max_age"] == sess.ttl_seconds


def test_session_cookie_clear_kwargs_zero_max_age():
    sess = SessionCookieService(secret=SESSION_SECRET)
    kwargs = sess.build_clear_cookie_kwargs()
    assert kwargs["max_age"] == 0
    assert kwargs["value"] == ""


def test_default_session_cookie_name():
    assert DEFAULT_SESSION_COOKIE_NAME == "mars_session"


# ---------------------------------------------------------------------------
# InMemoryEmailSender + ResendEmailSender
# ---------------------------------------------------------------------------


def test_in_memory_sender_records_every_call():
    sender = InMemoryEmailSender()

    async def _go():
        await sender.send(
            to="pedro@example.com", subject="hello", body_text="world"
        )
        await sender.send(
            to="other@example.com", subject="subject 2", body_text="body 2"
        )

    asyncio.run(_go())
    assert len(sender.outbox) == 2
    assert sender.outbox[0].to == "pedro@example.com"
    assert sender.outbox[1].subject == "subject 2"


def test_resend_sender_posts_expected_shape_to_resend_api():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "email-id-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ResendEmailSender(
        api_key="re_test", from_address="mars@example.com", client=client
    )

    async def _go():
        await sender.send(
            to="pedro@example.com",
            subject="hello",
            body_text="click the link",
        )

    asyncio.run(_go())
    assert seen["method"] == "POST"
    assert "/emails" in seen["url"]
    assert seen["auth"] == "Bearer re_test"


def test_resend_sender_raises_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad from address")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ResendEmailSender(
        api_key="re_test", from_address="bad@example.com", client=client
    )

    async def _go():
        await sender.send(
            to="pedro@example.com", subject="x", body_text="y"
        )

    with pytest.raises(EmailSendError, match="400"):
        asyncio.run(_go())


def test_resend_sender_rejects_empty_api_key():
    with pytest.raises(ValueError):
        ResendEmailSender(api_key="", from_address="x@y.com")


def test_resend_sender_rejects_empty_from_address():
    with pytest.raises(ValueError):
        ResendEmailSender(api_key="re_test", from_address="")


# ---------------------------------------------------------------------------
# End-to-end auth routes against create_control_app
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_app():
    store = EventStore(":memory:")
    store.init()
    sink = SSEEventSink()
    magic = MagicLinkService(secret=MAGIC_SECRET)
    # cookie_secure=False so TestClient's http:// transport will
    # forward the cookie on subsequent requests.
    sess = SessionCookieService(secret=SESSION_SECRET, cookie_secure=False)
    sender = InMemoryEmailSender()
    app = create_control_app(
        store=store,
        event_secret="ingest-secret",
        sink=sink,
        magic_link_service=magic,
        session_service=sess,
        email_sender=sender,
        magic_link_base_url="https://mars-control.example",
    )
    return app, sender, magic, sess


def test_request_magic_link_sends_email(auth_app):
    app, sender, _magic, _sess = auth_app
    with TestClient(app) as c:
        resp = c.post(
            "/auth/magic-link", json={"email": "pedro@example.com"}
        )
        assert resp.status_code == 202
        assert resp.json() == {"status": "sent", "email": "pedro@example.com"}
    assert len(sender.outbox) == 1
    body = sender.outbox[0].body_text
    assert "https://mars-control.example/auth/verify?token=" in body
    assert "15 minutes" in body


def test_verify_magic_link_sets_session_cookie(auth_app):
    app, sender, magic, sess = auth_app
    with TestClient(app) as c:
        # Request the magic link to seed the outbox
        c.post("/auth/magic-link", json={"email": "pedro@example.com"})
        assert len(sender.outbox) == 1
        body = sender.outbox[0].body_text
        # Pull the token out of the link
        token = body.split("token=")[1].split()[0]

        # Verify it
        resp = c.post("/auth/magic-link/verify", json={"token": token})
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == "pedro@example.com"
        # Session cookie set on the response
        cookie_header = resp.headers.get("set-cookie", "")
        assert "mars_session=" in cookie_header
        assert "HttpOnly" in cookie_header
        # (Secure flag verified in the production-cookie test below)


def test_production_session_cookie_is_secure():
    """Production cookie_secure=True must set the Secure flag."""
    sess = SessionCookieService(secret=SESSION_SECRET)  # default cookie_secure=True
    kwargs = sess.build_set_cookie_kwargs("t")
    assert kwargs["secure"] is True


def test_verify_magic_link_rejects_reused_token(auth_app):
    app, sender, magic, sess = auth_app
    with TestClient(app) as c:
        c.post("/auth/magic-link", json={"email": "pedro@example.com"})
        body = sender.outbox[-1].body_text
        token = body.split("token=")[1].split()[0]

        first = c.post("/auth/magic-link/verify", json={"token": token})
        assert first.status_code == 200

        second = c.post("/auth/magic-link/verify", json={"token": token})
        assert second.status_code == 401
        assert "consumed" in second.json()["detail"]


def test_verify_magic_link_rejects_garbage_token(auth_app):
    app, _, _, _ = auth_app
    with TestClient(app) as c:
        resp = c.post("/auth/magic-link/verify", json={"token": "not.a.jwt"})
        assert resp.status_code == 401


def test_protected_route_requires_session_cookie(auth_app):
    app, _, _, _ = auth_app
    with TestClient(app) as c:
        resp = c.get("/me")
        assert resp.status_code == 401
        assert "session cookie missing" in resp.json()["detail"]


def test_me_returns_user_after_full_signin_flow(auth_app):
    app, sender, _, _ = auth_app
    with TestClient(app) as c:
        c.post("/auth/magic-link", json={"email": "pedro@example.com"})
        body = sender.outbox[-1].body_text
        token = body.split("token=")[1].split()[0]
        c.post("/auth/magic-link/verify", json={"token": token})
        resp = c.get("/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == "pedro@example.com"


def test_logout_clears_cookie(auth_app):
    app, sender, _, _ = auth_app
    with TestClient(app) as c:
        c.post("/auth/magic-link", json={"email": "pedro@example.com"})
        body = sender.outbox[-1].body_text
        token = body.split("token=")[1].split()[0]
        c.post("/auth/magic-link/verify", json={"token": token})
        # Logout returns a cookie with max_age=0
        resp = c.post("/auth/logout")
        assert resp.status_code == 200
        assert "mars_session=" in resp.headers.get("set-cookie", "")


def test_app_without_auth_services_returns_503_on_auth_endpoints():
    """Environments that don't configure auth (local dev) should
    fail loudly on the auth endpoints, not silently accept."""
    store = EventStore(":memory:")
    store.init()
    app = create_control_app(
        store=store,
        event_secret="ingest-secret",
        # no magic_link_service, no session_service, no email_sender
    )
    with TestClient(app) as c:
        resp = c.post(
            "/auth/magic-link", json={"email": "pedro@example.com"}
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]


def test_invalid_email_format_returns_422(auth_app):
    app, _, _, _ = auth_app
    with TestClient(app) as c:
        resp = c.post("/auth/magic-link", json={"email": "not-an-email"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Story 9.2 — rate limit on /auth/magic-link
# ---------------------------------------------------------------------------


def test_magic_link_rate_limit_returns_429_over_cap():
    """Per-IP rate limit: burst past the configured cap returns 429
    with a Retry-After header. Only the first N emails go out."""
    from mars_control.auth.rate_limit import RateLimiter

    store = EventStore(":memory:")
    store.init()
    magic = MagicLinkService(secret=MAGIC_SECRET)
    sess = SessionCookieService(secret=SESSION_SECRET, cookie_secure=False)
    sender = InMemoryEmailSender()
    # Tight cap for testing: 2 requests per 60s
    limiter = RateLimiter(max_requests=2, window_seconds=60.0)
    app = create_control_app(
        store=store,
        event_secret="ingest-secret",
        magic_link_service=magic,
        session_service=sess,
        email_sender=sender,
        magic_link_rate_limiter=limiter,
    )
    with TestClient(app) as c:
        r1 = c.post("/auth/magic-link", json={"email": "a@example.com"})
        r2 = c.post("/auth/magic-link", json={"email": "b@example.com"})
        r3 = c.post("/auth/magic-link", json={"email": "c@example.com"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 429
    assert "too many magic-link requests" in r3.json()["detail"]
    assert "Retry-After" in r3.headers
    assert int(r3.headers["Retry-After"]) >= 1

    # Only the first two emails went out
    assert len(sender.outbox) == 2


def test_magic_link_rate_limit_default_is_5_per_minute(auth_app):
    """The default limiter allows 5 requests and rejects the 6th."""
    app, sender, _, _ = auth_app
    with TestClient(app) as c:
        # 5 accepted
        for i in range(5):
            r = c.post("/auth/magic-link", json={"email": f"u{i}@example.com"})
            assert r.status_code == 202, f"request {i} rejected: {r.text}"
        # 6th over cap
        r6 = c.post("/auth/magic-link", json={"email": "six@example.com"})
        assert r6.status_code == 429
    assert len(sender.outbox) == 5
