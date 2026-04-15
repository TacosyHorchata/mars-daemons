"""Backward-compatibility shim — schema.py was renamed to config.py
in v0.6 (Phase 1h). External callers doing `from mars_runtime.schema
import AgentConfig` continue to work; new code should import from
`mars_runtime.config` (or, for embedding, `mars_runtime.api`).

This shim has no maintenance overhead — it just re-exports the symbols
config.py defines. Plan to remove in a future major version after a
deprecation cycle.
"""

from __future__ import annotations

from .config import *  # noqa: F401,F403
from .config import AgentConfig  # explicit so static analysis sees it

__all__ = ["AgentConfig"]
