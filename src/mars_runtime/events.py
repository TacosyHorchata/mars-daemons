"""Event emission. One line of JSON per event, printed to stdout.

Consumers (mars-control, CLI pretty-printer, log collectors) tail stdout
and parse line-by-line. No envelope, no sequence, no durability split —
the process boundary IS the durability boundary.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


def emit(event_type: str, **fields: Any) -> None:
    line = json.dumps(
        {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **fields,
        },
        default=str,
    )
    print(line, flush=True, file=sys.stdout)
