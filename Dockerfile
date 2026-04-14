# mars-runtime container
# ----------------------
# Entrypoint: `python -m mars_runtime <agent.yaml> | --resume <id> | --list`.
# Reads user turns from stdin, emits JSON-line events to stdout.
#
# Volumes (mount a Fly volume at /data for persistence):
#   /data/workspace/          git repo, agent cwd
#   /data/sessions/           session snapshots (supervisor-only)
#
# Env:
#   ANTHROPIC_API_KEY — required
#   MARS_DATA_DIR     — defaults to /data
#   anything in AgentConfig.env — forwarded by the deploy layer

FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/TacosyHorchata/mars-daemons"
LABEL org.opencontainers.image.title="mars-runtime"
LABEL org.opencontainers.image.description="Minimal agent runtime — one script, Anthropic backend."

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# tini for clean SIGTERM forwarding. ripgrep powers the `grep` tool (falls
# back to pure-python if missing, but rg is ~100x faster on large trees).
# git is required by the supervisor for per-turn workspace commits.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tini ripgrep git; \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "anthropic>=0.40" \
        "openai>=1.0" \
        "httpx>=0.28,<1" \
        "pydantic>=2.6,<3" \
        "pyyaml>=6.0,<7"

RUN useradd -m -u 1000 -s /bin/bash mars

WORKDIR /app
COPY --chown=mars:mars src /app/runtime-src

# /data is the mount point for the per-entorno Fly volume. If no volume is
# mounted (e.g. `docker run` without -v), these dirs just live inside the
# container filesystem and disappear on exit.
RUN mkdir -p /data/workspace /data/sessions && chown -R mars:mars /data

USER mars

ENV PYTHONPATH="/app/runtime-src" \
    MARS_DATA_DIR="/data"

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "mars_runtime._bootstrap"]
