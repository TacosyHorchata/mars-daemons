"""Local-dev entrypoint for the Mars control plane.

This module exists ONLY for local Fly emulation — it is never loaded
by production ``uvicorn --factory`` calls. It constructs a
:class:`create_control_app` with:

* ``InMemoryEmailSender`` (so magic-link emails land in an in-memory
  outbox instead of hitting Resend — inspect via ``GET /dev/outbox``)
* hardcoded dev secrets (short, clearly-marked "do not use")
* ``cookie_secure=False`` so the session cookie works over
  ``http://localhost``
* ``default_supervisor_url=http://localhost:8080`` to proxy
  ``GET /sessions`` and ``POST /sessions/{id}/input`` to a locally-
  running ``mars-runtime`` supervisor
* CORS permitted from ``http://localhost:3000`` so the Next.js
  frontend dev server can talk to us cross-origin

Run with::

    cd apps/mars-control/backend
    uvicorn --factory mars_control.local_server:create_local_app --port 8000

The environment variables on ``MARS_LOCAL_*`` override the defaults
when you need to point at a different supervisor or frontend origin.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from fastapi import FastAPI

from mars_control.api.routes import create_control_app
from mars_control.auth.email import InMemoryEmailSender
from mars_control.auth.magic_link import MagicLinkService
from mars_control.auth.session import SessionCookieService

__all__ = ["create_local_app"]

_LOCAL_MAGIC_LINK_SECRET = "local-dev-magic-link-secret-do-not-use-in-prod"
_LOCAL_SESSION_SECRET = "local-dev-session-secret-do-not-use-in-prod-ever"
_LOCAL_EVENT_SECRET = "local-dev-event-secret"

_PROD_MARKERS = ("MARS_ENV", "FLY_APP_NAME", "FLY_REGION", "FLY_MACHINE_ID")


def _refuse_if_prod_environment() -> None:
    """Abort if this module is imported from anything that looks
    like a production environment. Codex flagged the hardcoded
    secrets + /dev/outbox as a real footgun; we want any accidental
    ``uvicorn mars_control.local_server`` on Fly to crash loud rather
    than serve magic links to the public internet.
    """
    tripped = [name for name in _PROD_MARKERS if os.environ.get(name)]
    if tripped:
        raise RuntimeError(
            "mars_control.local_server is dev-only and refuses to run in "
            "environments that look like production (found env vars: "
            f"{', '.join(tripped)}). Use mars_control.api.routes.create_control_app "
            "directly with production-provisioned secrets."
        )


def create_local_app() -> FastAPI:
    """Build a control-plane FastAPI app wired for local Fly emulation."""
    _refuse_if_prod_environment()
    print(
        "\n"
        "============================================================\n"
        "  mars-control LOCAL DEV SERVER — hardcoded dev secrets,\n"
        "  InMemoryEmailSender, cookie_secure=False, /dev/outbox open.\n"
        "  NEVER bind this to a non-loopback interface.\n"
        "============================================================\n",
        file=sys.stderr,
        flush=True,
    )
    outbox = InMemoryEmailSender()
    magic = MagicLinkService(secret=_LOCAL_MAGIC_LINK_SECRET)
    session = SessionCookieService(
        secret=_LOCAL_SESSION_SECRET,
        cookie_secure=False,
    )

    supervisor_url = os.environ.get(
        "MARS_LOCAL_SUPERVISOR_URL", "http://localhost:8080"
    )
    frontend_url = os.environ.get("MARS_LOCAL_FRONTEND_URL", "http://localhost:3000")

    app = create_control_app(
        magic_link_service=magic,
        session_service=session,
        email_sender=outbox,
        magic_link_base_url=frontend_url,
        default_supervisor_url=supervisor_url,
        cors_allow_origins=[frontend_url],
        event_secret=_LOCAL_EVENT_SECRET,
    )

    # Stash the outbox on app.state so /dev/outbox can introspect it.
    app.state.dev_outbox = outbox

    @app.get("/dev/outbox", tags=["dev"])
    async def dev_outbox() -> dict[str, Any]:
        """Read-only dump of the in-memory email outbox.

        Mirrors the InMemoryEmailSender's sent messages so local dev
        can copy the magic-link URL from a browser tab instead of
        running tail on supervisor logs.
        """
        messages = [
            {
                "to": msg.to,
                "subject": msg.subject,
                "body_text": msg.body_text,
            }
            for msg in outbox.outbox
        ]
        return {"count": len(messages), "messages": messages}

    @app.post("/dev/outbox/clear", tags=["dev"])
    async def dev_outbox_clear() -> dict[str, Any]:
        """Nuke the outbox — useful between manual test runs."""
        count = len(outbox.outbox)
        outbox.outbox.clear()
        return {"cleared": count}

    return app
