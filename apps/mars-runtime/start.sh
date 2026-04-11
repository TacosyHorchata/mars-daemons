#!/usr/bin/env bash
# Mars runtime container entrypoint.
#
# Starts the supervisor FastAPI app via uvicorn. tini (the image
# ENTRYPOINT) forwards SIGTERM here, and uvicorn's lifespan-shutdown
# then fires the mars-runtime ``SessionManager.shutdown()`` which
# SIGKILLs every live ``claude`` subprocess.

set -euo pipefail

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
LOG_LEVEL="${UVICORN_LOG_LEVEL:-info}"

# Single worker is MANDATORY in v1. The supervisor's SessionManager
# owns its ``active_sessions`` dict in-memory; multi-worker uvicorn
# would split session state across processes and the /sessions
# endpoints would see different worlds depending on which process
# served the request. When Epic 5 adds persistent session handles on
# the Fly volume, we can revisit.
WORKERS=1

echo "mars-runtime: starting supervisor on ${HOST}:${PORT}"
echo "mars-runtime: claude_code version=$(claude --version 2>&1 || echo 'unknown')"

# --factory so uvicorn calls create_app() — the factory wires the
# SessionManager + default spawn_fn.
exec python -m uvicorn \
    --factory \
    --host "${HOST}" \
    --port "${PORT}" \
    --workers "${WORKERS}" \
    --log-level "${LOG_LEVEL}" \
    "supervisor:create_app"
