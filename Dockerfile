# mars-runtime container
# ----------------------
# Entrypoint: `python -m mars_runtime /workspace/agent.yaml`. The process
# reads user turns from stdin and emits JSON-line events to stdout.
#
# Env contract:
#   * ANTHROPIC_API_KEY — required
#   * MARS_AGENT_YAML   — optional path override (default /workspace/agent.yaml)
#   * anything declared in AgentConfig.env — forwarded by the deploy layer

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
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tini ripgrep; \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "anthropic>=0.40" \
        "httpx>=0.28,<1" \
        "pydantic>=2.6,<3" \
        "pyyaml>=6.0,<7"

RUN useradd -m -u 1000 -s /bin/bash mars

WORKDIR /app
COPY --chown=mars:mars src /app/runtime-src

# Workspace dir where each daemon's agent.yaml + CLAUDE.md live. Mounted
# or populated at deploy time by `mars deploy`.
RUN mkdir -p /workspace && chown -R mars:mars /workspace

USER mars

ENV PYTHONPATH="/app/runtime-src" \
    MARS_AGENT_YAML="/workspace/agent.yaml"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "python -m mars_runtime \"$MARS_AGENT_YAML\""]
