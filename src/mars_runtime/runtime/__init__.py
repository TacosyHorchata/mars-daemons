"""Runtime subpackage — the agent loop + session orchestration.

Exports the core entry `run()` from `agent_loop` so callers can
`from mars_runtime.runtime import run` without knowing the module layout.
"""

from .agent_loop import run

__all__ = ["run"]
