"""User-input queue adapter — turns the RPC-driven Queue into the
iterable that agent.run() expects for `turn_source`."""

from __future__ import annotations

import queue


def _user_input_stream(q: queue.Queue) -> "object":
    """Yields user input lines from the queue until EOF sentinel (None)."""
    while True:
        item = q.get()
        if item is None:
            return
        yield item
