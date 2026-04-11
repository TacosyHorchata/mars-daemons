#!/usr/bin/env bash
# spikes/01-claude-code-oauth.sh
#
# Spike 1: Claude Code headless OAuth
# ------------------------------------
# Validates that `claude -p --output-format stream-json` can run headlessly
# (a) on the host using existing Claude Max login, and
# (b) in a Docker container using a CLAUDE_CODE_OAUTH_TOKEN env var produced
#     by `claude setup-token` (Pedro's interactive browser OAuth).
#
# Pinned Claude Code version: 2.1.101 (match what was validated on host).
#
# Validation status as of 2026-04-10:
#   [x] host stream-json headless — WORKS (captured fixture in spike 2)
#   [ ] `claude setup-token` browser flow — REQUIRES Pedro (interactive)
#   [ ] container run with CLAUDE_CODE_OAUTH_TOKEN — PENDING Pedro's token
#
# Usage:
#   ./spikes/01-claude-code-oauth.sh host        # re-run host validation
#   ./spikes/01-claude-code-oauth.sh setup       # run `claude setup-token` (browser)
#   ./spikes/01-claude-code-oauth.sh container   # build image + run with $CLAUDE_CODE_OAUTH_TOKEN
#   ./spikes/01-claude-code-oauth.sh all         # host → (manual setup) → container

set -euo pipefail

CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.101}"
IMAGE_TAG="${IMAGE_TAG:-mars-spike-01:latest}"

host_check() {
  echo "=== host stream-json headless check ==="
  # Run in a clean-ish env so we're not leaning on a parent Claude Code session.
  env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_EXECPATH -u CMUX_CLAUDE_PID \
    claude -p "say hi in one word" \
      --output-format stream-json \
      --verbose \
    | tee /tmp/mars-spike-01-host.jsonl \
    | head -20
  echo "--- full output saved to /tmp/mars-spike-01-host.jsonl"
  echo "--- success criterion: at least one line of type 'result' with subtype 'success'"
  grep -q '"type":"result"' /tmp/mars-spike-01-host.jsonl && echo "PASS: result event present"
}

setup_token() {
  echo "=== claude setup-token (INTERACTIVE — browser will open) ==="
  echo "After login, the CLI prints a long-lived token."
  echo "Export it in your shell:   export CLAUDE_CODE_OAUTH_TOKEN=<pasted-token>"
  claude setup-token
}

container_check() {
  if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    echo "ERROR: CLAUDE_CODE_OAUTH_TOKEN is not set. Run '$0 setup' first and export the token." >&2
    exit 2
  fi
  echo "=== docker build ==="
  workdir="$(mktemp -d)"
  cat > "$workdir/Dockerfile" <<EOF
FROM node:20-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION}
RUN claude --version
WORKDIR /workspace
CMD ["claude", "--version"]
EOF
  docker build -t "$IMAGE_TAG" "$workdir"
  rm -rf "$workdir"

  echo "=== docker run: claude --version (no auth needed) ==="
  docker run --rm "$IMAGE_TAG" claude --version

  echo "=== docker run: claude -p headless with CLAUDE_CODE_OAUTH_TOKEN ==="
  docker run --rm \
    -e CLAUDE_CODE_OAUTH_TOKEN \
    "$IMAGE_TAG" \
    claude -p "say hi in one word" --output-format stream-json --verbose \
    | tee /tmp/mars-spike-01-container.jsonl \
    | head -20
  echo "--- full output saved to /tmp/mars-spike-01-container.jsonl"
  grep -q '"type":"result"' /tmp/mars-spike-01-container.jsonl \
    && echo "PASS: container headless stream-json works" \
    || { echo "FAIL: no result event"; exit 1; }
}

case "${1:-host}" in
  host) host_check ;;
  setup) setup_token ;;
  container) container_check ;;
  all) host_check; setup_token; container_check ;;
  *) echo "Usage: $0 {host|setup|container|all}" >&2; exit 1 ;;
esac
