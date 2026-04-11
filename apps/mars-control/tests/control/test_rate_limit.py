"""Unit tests for :mod:`mars_control.auth.rate_limit` (Story 9.2)."""

from __future__ import annotations

import pytest

from mars_control.auth.rate_limit import (
    DEFAULT_MAGIC_LINK_MAX_REQUESTS,
    DEFAULT_MAGIC_LINK_WINDOW_SECONDS,
    RateLimiter,
)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_defaults_are_5_per_minute():
    assert DEFAULT_MAGIC_LINK_MAX_REQUESTS == 5
    assert DEFAULT_MAGIC_LINK_WINDOW_SECONDS == 60.0


def test_invalid_max_requests_rejected():
    with pytest.raises(ValueError):
        RateLimiter(max_requests=0)
    with pytest.raises(ValueError):
        RateLimiter(max_requests=-1)


def test_invalid_window_seconds_rejected():
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=0)
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=-1)


# ---------------------------------------------------------------------------
# check() — allow up to max, then deny
# ---------------------------------------------------------------------------


def test_allows_exactly_max_requests_within_window():
    t = [1000.0]
    rl = RateLimiter(max_requests=3, window_seconds=60.0, clock=lambda: t[0])
    assert rl.check("1.2.3.4") is True
    assert rl.check("1.2.3.4") is True
    assert rl.check("1.2.3.4") is True
    assert rl.check("1.2.3.4") is False  # cap reached


def test_different_keys_have_independent_buckets():
    rl = RateLimiter(max_requests=2, window_seconds=60.0, clock=lambda: 1.0)
    assert rl.check("alice") is True
    assert rl.check("alice") is True
    assert rl.check("alice") is False
    # bob is untouched
    assert rl.check("bob") is True
    assert rl.check("bob") is True
    assert rl.check("bob") is False


def test_denied_request_is_not_recorded():
    """A rejected check must NOT count toward the window — otherwise
    an attacker spamming the endpoint could keep the window pinned
    indefinitely."""
    t = [1000.0]
    rl = RateLimiter(max_requests=2, window_seconds=60.0, clock=lambda: t[0])
    assert rl.check("x") is True
    assert rl.check("x") is True
    # Now at cap — further checks denied
    for _ in range(10):
        assert rl.check("x") is False
    # Advance past the window; the two original checks expire
    t[0] += 61
    assert rl.check("x") is True
    assert rl.check("x") is True
    assert rl.check("x") is False


def test_window_slides_forward_as_time_advances():
    """Sliding window semantics: each recorded timestamp expires
    independently ``window_seconds`` after it was recorded. Checks
    spread across time see each one fall off the back of the queue."""
    t = [1000.0]
    rl = RateLimiter(max_requests=3, window_seconds=60.0, clock=lambda: t[0])
    # Spread 3 requests across 20s so they age differently
    for offset in (0, 10, 20):
        t[0] = 1000.0 + offset
        assert rl.check("ip") is True
    # Cap reached at t=1020
    assert rl.check("ip") is False

    # t=1030: still all three inside the window
    t[0] = 1030.0
    assert rl.check("ip") is False

    # t=1061: first request (at 1000) is now 61s old, expired.
    # The other two (at 1010 and 1020) are still inside, so we have
    # room for ONE more. The check records a new timestamp.
    t[0] = 1061.0
    assert rl.check("ip") is True
    # Now at 3 again (the two survivors + the new one) → denied
    assert rl.check("ip") is False

    # t=1071: the 1010 timestamp expired (10 seconds stale), leaving
    # 1020, 1061, + room for one more.
    t[0] = 1071.0
    assert rl.check("ip") is True


# ---------------------------------------------------------------------------
# retry_after_seconds
# ---------------------------------------------------------------------------


def test_retry_after_is_zero_when_below_limit():
    rl = RateLimiter(max_requests=5, window_seconds=60.0, clock=lambda: 1.0)
    rl.check("x")
    assert rl.retry_after_seconds("x") == 0.0


def test_retry_after_matches_oldest_timestamp_plus_window():
    t = [1000.0]
    rl = RateLimiter(max_requests=2, window_seconds=60.0, clock=lambda: t[0])
    rl.check("x")  # recorded at t=1000
    t[0] = 1005.0
    rl.check("x")  # recorded at t=1005
    # cap reached
    assert rl.check("x") is False
    # retry_after from the oldest (t=1000): (1000 + 60) - 1005 = 55
    assert rl.retry_after_seconds("x") == 55.0


def test_retry_after_zero_for_unknown_key():
    rl = RateLimiter()
    assert rl.retry_after_seconds("nobody") == 0.0


# ---------------------------------------------------------------------------
# reset() + observability
# ---------------------------------------------------------------------------


def test_reset_single_key():
    rl = RateLimiter(max_requests=1, window_seconds=60.0, clock=lambda: 1.0)
    rl.check("x")
    rl.check("y")
    assert rl.active_keys() == 2
    rl.reset("x")
    assert rl.active_keys() == 1
    # x is now fresh — can check again
    assert rl.check("x") is True


def test_reset_all_keys():
    rl = RateLimiter(max_requests=1, window_seconds=60.0, clock=lambda: 1.0)
    rl.check("alice")
    rl.check("bob")
    rl.reset()
    assert rl.active_keys() == 0
    assert rl.check("alice") is True
    assert rl.check("bob") is True


def test_active_keys_does_not_count_expired_buckets_lazily():
    """The limiter does not proactively sweep expired buckets, but
    check() evicts them lazily. active_keys() reflects the whole
    dict, including buckets with 0 timestamps (not swept). Document
    the current behavior."""
    t = [1000.0]
    rl = RateLimiter(max_requests=1, window_seconds=10.0, clock=lambda: t[0])
    rl.check("x")
    t[0] = 1100.0  # far past window
    # active_keys still counts the stale bucket until a check evicts it
    assert rl.active_keys() == 1
    rl.check("x")  # evicts x's stale timestamps and records a new one
    assert rl.active_keys() == 1
