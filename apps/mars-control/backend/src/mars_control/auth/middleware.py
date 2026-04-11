"""FastAPI dependency that resolves the current session user.

Reads the session cookie off the incoming request, verifies it via
:class:`~mars_control.auth.session.SessionCookieService`, and returns
a :class:`~mars_control.auth.session.SessionUser`.

On failure the dependency raises ``HTTPException(401)`` so protected
routes get a 401 response body with a clean ``WWW-Authenticate``
header the frontend uses to redirect to the signup page.
"""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request, status

from mars_control.auth.session import (
    SessionCookieService,
    SessionError,
    SessionUser,
)

__all__ = [
    "AuthDependency",
    "make_current_user_dependency",
]

AuthDependency = Callable[[Request], SessionUser]


def make_current_user_dependency(
    session_service: SessionCookieService,
) -> AuthDependency:
    """Build a FastAPI dependency bound to a specific session service.

    Usage in a FastAPI app::

        current_user = make_current_user_dependency(session_service)

        @app.get("/me")
        async def me(user: SessionUser = Depends(current_user)) -> dict:
            return {"email": user.email}
    """

    def _current_user(request: Request) -> SessionUser:
        token = request.cookies.get(session_service.cookie_name)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="session cookie missing",
                headers={"WWW-Authenticate": 'Cookie realm="mars"'},
            )
        try:
            return session_service.verify(token)
        except SessionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": 'Cookie realm="mars"'},
            ) from exc

    return _current_user
