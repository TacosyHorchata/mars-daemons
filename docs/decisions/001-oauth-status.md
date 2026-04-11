# Decision 001 — Claude Code headless OAuth status

**Date:** 2026-04-10
**Status:** Host stream-json validated; container + token path scripted but pending Pedro's interactive `claude setup-token` browser flow
**Decision:** Proceed with Epic 1. Architecture commitment is auth-mode-agnostic.

## What was validated on 2026-04-10

On the host machine with Pedro's existing Claude Max login, the following
command was run in a clean subshell (parent Claude Code session env vars
unset):

```bash
env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_EXECPATH -u CMUX_CLAUDE_PID \
  claude -p "say hi in one word" --output-format stream-json --verbose
```

Produced a well-formed JSONL stream containing:

- `{"type":"system","subtype":"init", ...}` — session metadata (`cwd`, `session_id`, `model`, `tools[]`, `mcp_servers[]`, `permissionMode`, `claude_code_version`, `apiKeySource`)
- `{"type":"assistant", ...}` — messages with `content` array of `text` / `thinking` / `tool_use` blocks
- `{"type":"user", ...}` — `tool_result` payloads
- `{"type":"rate_limit_event", ...}` — quota + overage status
- `{"type":"result","subtype":"success", ...}` — final usage, cost, `permission_denials[]`, `duration_ms`

A richer second run (with `--allowed-tools Bash --permission-mode acceptEdits`
asking the model to execute a `Bash` tool call) produced the full canonical
sequence `system_init → assistant(thinking) → assistant(tool_use) → user(tool_result) → assistant(text) → result` — this is the fixture captured in spike 2.

## What is NOT yet validated

1. `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` export
2. `claude -p --output-format stream-json` running **inside a Docker container** with only that token for auth (no host `~/.claude/` mount)

Both items require Pedro to click through the browser OAuth flow once to mint a long-lived token. The reproducible script at `spikes/01-claude-code-oauth.sh` handles this end-to-end when Pedro runs `./spikes/01-claude-code-oauth.sh setup` followed by `./spikes/01-claude-code-oauth.sh container`.

Docker daemon was not running on the build machine during this session, so the container build step was not exercised in-session.

## Why it is safe to proceed with Epic 1 now

The architecture commitment for Mars v1 — stream-json parsing, event forwarding, multi-session supervisor, SSE fan-out on control plane — is **auth-mode-agnostic**. The supervisor reads JSONL from a subprocess stdout regardless of whether the CLI was authenticated via a long-lived OAuth token or an API key.

If Pedro runs the container spike and it fails, the pivot is bounded:
- replace `CLAUDE_CODE_OAUTH_TOKEN` with `ANTHROPIC_API_KEY` (BYO-API-key) in `apps/mars-runtime/Dockerfile` and the Fly secrets step of `mars deploy`
- update the marketing story ("bring your Claude Max subscription" → "bring your API key")
- `docs/security.md` updates its "auth model" section accordingly

No parser, forwarder, or supervisor code changes in that pivot. Epic 1's highest-risk file (`session/claude_code_stream.py`) does not depend on the auth mode.

## Action Pedro can take any time

```bash
cd /Users/pedrorios/Desktop/mars-daemons
./spikes/01-claude-code-oauth.sh setup          # browser OAuth → copy token
export CLAUDE_CODE_OAUTH_TOKEN=<paste>
# make sure Docker Desktop is running
./spikes/01-claude-code-oauth.sh container      # build + run + assert result event
```

Expected on success: a `result` event is printed and the script exits 0.
On failure: the script exits non-zero and the JSONL tail indicates the auth error; the BYO-API-key pivot plan above is triggered.
