"""Tiny in-memory sliding-window rate limiter.

Scope (Story 9.2):

* v1 runs on a single control-plane host so per-process state is
  authoritative. Multi-host rate limiting is deferred to v2 (Redis
  or similar).
* Used on ``POST /auth/magic-link`` to prevent an attacker from
  spamming a victim's inbox with sign-in links, and to make bulk
  token enumeration unattractive.
* Keyed by client IP (``request.client.host``). Behind a trusted
  proxy, callers must forward the real client IP via
  ``X-Forwarded-For`` and the proxy must not be attacker-controlled.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque

__all__ = [
    "DEFAULT_MAGIC_LINK_MAX_REQUESTS",
    "DEFAULT_MAGIC_LINK_WINDOW_SECONDS",
    "RateLimiter",
]

#: v1 defaults for the magic-link endpoint. 5 requests / 60s / IP
#: is enough for a human on a flaky email client to retry a few
#: times without being blocked, but low enough to slow down bulk
#: spam.
DEFAULT_MAGIC_LINK_MAX_REQUESTS = 5
DEFAULT_MAGIC_LINK_WINDOW_SECONDS = 60.0


@dataclass
class _BucketState:
    """Per-key sliding-window state."""

    #: Monotonic timestamps of every accepted request inside the window.
    timestamps: Deque[float] = field(default_factory=deque)


class RateLimiter:
    """In-memory sliding-window rate limiter keyed by arbitrary string.

    Args:
        max_requests: Maximum successful requests allowed inside the
            ``window_seconds`` window per key.
        window_seconds: Window size in seconds.
        clock: Optional monotonic-time callable. Tests pin this for
            deterministic expiry behavior.

    The limiter cleans up expired entries lazily on every
    :meth:`check` call — the memory footprint scales with the number
    of *distinct keys seen in the last ``window_seconds``*, not with
    total request volume.
    """

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAGIC_LINK_MAX_REQUESTS,
        window_seconds: float = DEFAULT_MAGIC_LINK_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_requests <= 0:
            raise ValueError(f"max_requests must be positive, got {max_requests}")
        if window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be positive, got {window_seconds}"
            )
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _BucketState] = {}

    @property
    def max_requests(self) -> int:
        return self._max

    @property
    def window_seconds(self) -> float:
        return self._window

    def check(self, key: str) -> bool:
        """Return ``True`` if the request is allowed.

        On ``True``, the caller's timestamp is recorded (counts
        toward future checks). On ``False``, nothing is recorded —
        the caller is already over the limit.
        """
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _BucketState()
            self._buckets[key] = bucket

        # Evict timestamps outside the window
        cutoff = now - self._window
        while bucket.timestamps and bucket.timestamps[0] <= cutoff:
            bucket.timestamps.popleft()

        if len(bucket.timestamps) >= self._max:
            return False

        bucket.timestamps.append(now)
        return True

    def retry_after_seconds(self, key: str) -> float:
        """Return how long the caller must wait until the next request
        would be allowed. Returns ``0`` if the bucket is below the limit.

        Useful for ``Retry-After`` HTTP headers.
        """
        bucket = self._buckets.get(key)
        if bucket is None or len(bucket.timestamps) < self._max:
            return 0.0
        oldest = bucket.timestamps[0]
        return max(0.0, (oldest + self._window) - self._clock())

    def reset(self, key: str | None = None) -> None:
        """Drop state for one key (or all keys if ``None``)."""
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)

    def active_keys(self) -> int:
        """Number of keys currently being tracked. For observability."""
        return len(self._buckets)
