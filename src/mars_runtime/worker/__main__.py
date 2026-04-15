"""`python -m mars_runtime.worker` entry point — spawned by the broker."""

from __future__ import annotations

import sys

from .main import main


if __name__ == "__main__":
    sys.exit(main())
