"""Launch the standalone HTTP host."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the mars-daemons backend")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", default=int(os.getenv("PORT", "8080")), type=int)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    uvicorn.run(
        "mars_runtime.host:create_app",
        host=args.host,
        port=args.port,
        factory=True,
        reload=args.reload,
    )
    return 0
