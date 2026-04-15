"""Shared path resolution. Read once, used by CLI and runtime.

Kept as an underscore-prefixed module so it's clearly internal; the
public config surface is `config.py` (AgentConfig + loading) and
`api.py` (embedding entrypoints).
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir(override: str | None = None) -> Path:
    """Resolve $MARS_DATA_DIR (or `override`) to an absolute Path."""
    raw = override or os.environ.get("MARS_DATA_DIR") or "./.mars-data"
    return Path(raw).resolve()
