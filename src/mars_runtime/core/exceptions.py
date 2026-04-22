"""Core exceptions for the agents module.

These exceptions are transport-agnostic. HTTP adapters (e.g. the FastAPI
router) translate them into framework-specific responses.
"""

from __future__ import annotations


class AgentsError(Exception):
    """Base exception for the agents module."""


class AuthenticationError(AgentsError):
    """Raised when authentication fails.

    ``status_code`` is a hint for HTTP adapters (401, 403, 503, etc.).
    Non-HTTP hosts can ignore it or map it however they choose.
    """

    def __init__(self, detail: str, *, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class FileSizeExceededError(AgentsError):
    """Raised when an uploaded file exceeds the configured size limit."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class OrgScopingError(AgentsError):
    """Raised when an operation targets a conversation owned by another org."""

    def __init__(self, detail: str = "Organization mismatch") -> None:
        super().__init__(detail)
        self.detail = detail


def serialize_exception_details(
    exc: BaseException,
    *,
    phase: str,
    tool: str | None = None,
    call_id: str | None = None,
    input_payload: object = None,
) -> dict:
    import traceback as _tb

    return {
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
        "traceback": "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)),
        "phase": phase,
        "tool": tool,
        "call_id": call_id,
        "input": input_payload,
    }
