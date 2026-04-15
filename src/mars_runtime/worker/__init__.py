"""Worker subpackage — the agent-loop process with no credential access.

Invocation (internal, not user-facing):
    python -m mars_runtime.worker
        --agent-json <json-encoded AgentConfig>
        --session-id <sess_*>
        --data-dir <abs path>
        [--start-messages-file <path to json>]

See broker.process.spawn_worker for the broker-side invocation.
"""

from .broker_client import BrokerDisconnected
from .main import main

__all__ = ["BrokerDisconnected", "main"]
