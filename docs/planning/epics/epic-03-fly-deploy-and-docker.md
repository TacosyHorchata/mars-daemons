# Epic 3 — Fly Deploy & Dockerization

**Status:** `[ ]` not started
**Days:** 5 (Dockerfile + security hooks + local container test) → 6 (mars deploy CLI + end-to-end Fly deploy + Codex runtime added)
**Depends on:** Epic 1 (needs supervisor) + Epic 2 (needs forwarder to talk to control plane)
**Downstream blockers:** Epic 5 (multi-session in a real VM), Epic 7 (dev dogfood requires real deploy)
**Risk level:** MEDIUM

## Summary

Package `mars-runtime` into a Docker image with pinned Claude Code + Codex CLIs and the `claude_code_settings.json` security hooks. Build the `mars deploy` CLI that creates a Fly.io app, launches a machine from the image, injects secrets, and returns a URL. Wire `mars ssh` as a thin wrapper around `flyctl ssh console`. Add Codex subprocess support as a parallel runtime.

## Context

The v1 data plane is one Fly machine per workspace. This epic turns the locally-running supervisor from Epic 1 into a container that Fly can schedule, and builds the CLI that makes deploying trivial (`mars deploy ./agent.yaml`). The `claude_code_settings.json` file is baked into the image and contains the PreToolUse hooks that enforce CLAUDE.md immutability and secret-read speed bumps — **this file IS the security model, not a config detail.**

Two spikes run during this epic: (4) validate machine can POST outbound to control plane, (5) measure cold-boot timing.

## Scope

### In scope
- `apps/mars-runtime/Dockerfile` — Multi-stage, Python 3.11-slim, non-root user, pinned Claude Code CLI, pinned Codex CLI, system deps. Template from `services/fastapi/Dockerfile` in Camtom.
- `apps/mars-runtime/claude_code_settings.json` ★ — PreToolUse hooks: block Edit/Write on `CLAUDE.md`/`AGENTS.md`, block `bash` commands matching `env|printenv|echo\s+\$`. Baked into image, read-only.
- `apps/mars-runtime/start.sh` — Container entrypoint (starts supervisor, handles graceful shutdown)
- `apps/mars-control/backend/src/fly/client.py` — Thin wrapper around Fly.io REST API (create app, create machine, set secrets, destroy)
- `packages/mars-cli/src/mars/deploy.py` — `mars deploy ./agent.yaml` command. Flow: parse agent.yaml → ensure Fly app exists for workspace → ensure machine exists → POST agent.yaml to supervisor → return chat URL.
- `packages/mars-cli/src/mars/ssh.py` — `mars ssh <agent-name>` that shells out to `flyctl ssh console -a <app-id>`. Zero custom SSH code.
- `apps/mars-runtime/src/session/codex.py` — Codex subprocess lifecycle (mirrors `claude_code.py` shape). Uses API key auth path if OAuth spike failed.
- `apps/mars-runtime/src/session/manager.py` — Extend to dispatch to `claude_code.py` or `codex.py` based on `agent.yaml` runtime field
- Spike 4: Fly machine outbound HTTP reachability test (1h, during Day 5)
- Spike 5: Fly machine cold-boot timing measurement (30min, during Day 5)

### Out of scope (deferred)
- Warm pool for fast cold starts (Epic 8 if spike 5 shows >30s, otherwise skipped)
- Auto-scaling / horizontal scaling (v2)
- Custom domains per workspace (v2)
- Multi-region deploys (v2 — single region for v1)

## Acceptance criteria

- [ ] `apps/mars-runtime/Dockerfile` builds successfully and produces an image <500MB
- [ ] Container runs supervisor on port 8080, responds to healthcheck at `/health`
- [ ] `claude_code_settings.json` is present in the image at the path Claude Code expects (`~/.config/claude/settings.json` or wherever the CLI reads it — verify in Spike 2 output)
- [ ] Running `docker run` locally with `CLAUDE_CODE_OAUTH_TOKEN` env var successfully spawns a daemon via the control API
- [ ] PreToolUse hook blocks `Edit` on `CLAUDE.md` (manually verified in a local container)
- [ ] PreToolUse hook blocks `bash echo $CLAUDE_CODE_OAUTH_TOKEN` (manually verified)
- [ ] `fly/client.py` exposes `create_app`, `create_machine`, `set_secrets`, `destroy_machine`, `list_machines` methods
- [ ] `mars deploy examples/pr-reviewer-agent.yaml` runs end-to-end: creates Fly app → launches machine → POSTs agent.yaml → returns URL
- [ ] The returned URL hits the control plane SSE endpoint (not the machine directly)
- [ ] `mars ssh pr-reviewer` opens a shell inside the Fly machine
- [ ] `agent.yaml` with `runtime: codex` successfully spawns a Codex subprocess (API key path)
- [ ] Spike 4: machine POSTs a test event to control plane's ingest endpoint with valid `X-Event-Secret`, persists in events table
- [ ] Spike 5: cold boot measurement documented in `docs/decisions/002-cold-boot-timing.md`

## Critical files

| File | Purpose |
|---|---|
| `apps/mars-runtime/Dockerfile` | Multi-stage image (Camtom template + CC/Codex CLIs) |
| `apps/mars-runtime/claude_code_settings.json` ★ | **Security model: PreToolUse hooks** |
| `apps/mars-runtime/start.sh` | Container entrypoint |
| `apps/mars-runtime/src/session/codex.py` | Codex runtime (mirrors claude_code.py) |
| `apps/mars-control/backend/src/fly/client.py` | Fly.io REST wrapper |
| `packages/mars-cli/src/mars/deploy.py` | `mars deploy` command |
| `packages/mars-cli/src/mars/ssh.py` | `mars ssh` wrapper around flyctl |
| `docs/decisions/002-cold-boot-timing.md` | Spike 5 findings |

## Dependencies

- **Upstream:** Epic 1 (supervisor), Epic 2 (forwarder config)
- **Downstream:**
  - Epic 5 (multi-session with real VM restart scenarios)
  - Epic 7 (Pedro's dogfood deploy)
  - Epic 8 (Maat template needs deploy machinery)

## Risks

| Risk | Mitigation |
|---|---|
| Docker image >2GB because of CLI installs | Multi-stage build, strip docs/man/locale, `apt-get clean`. Target <500MB. |
| Claude Code CLI installer needs interactive prompts during `docker build` | Use `CI=1` or `CLAUDE_NO_INTERACTIVE=1` env var. Research in Epic 0 spikes. |
| Fly API rate limits hit during rapid deploy testing | Batch deploys, don't loop in CI. Use a single test app during development. |
| PreToolUse hook format differs from what the spike discovered | Verify against actual Claude Code version docs. Test hooks manually in the container before acceptance. |
| Cold boot >30s breaks Maat onboarding | Spike 5 measures. If >30s, add warm pool task to Epic 8 scope. |
| Codex CLI requires interactive login on first use | API key path bypasses this. If OAuth for Codex works, great; if not, v1 is API key only for Codex. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] CI builds the Docker image and pushes to a registry (GHCR or Fly's registry)
- [ ] `mars deploy` works end-to-end against a real Fly.io project
- [ ] Cold boot time documented
- [ ] PreToolUse hooks verified working in a real deployed machine
- [ ] `mars ssh` works
- [ ] Both runtimes (Claude Code + Codex) can be spawned via the manager

## Stories (to be decomposed next cycle)

*Placeholder — next session will break this into ~5 stories:*
- Story 3.1: `Dockerfile` + `start.sh` + local container test
- Story 3.2: `claude_code_settings.json` PreToolUse hooks + manual verification
- Story 3.3: `fly/client.py` Fly REST wrapper + unit tests
- Story 3.4: `mars deploy` CLI end-to-end + `mars ssh` wrapper
- Story 3.5: `codex.py` runtime + session manager dispatch + spikes 4+5

## Notes

- **Camtom's Dockerfile at `services/fastapi/Dockerfile` is the template.** Copy the multi-stage structure, non-root user pattern, system deps pattern. Adapt for Claude Code + Codex installs.
- **Pin CLI versions in the Dockerfile**, not in a `.tool-versions` file. Explicit is better than implicit for reproducibility.
- **`claude_code_settings.json` format** — verify against Claude Code's current docs for `PreToolUse` hook syntax. The shape is roughly `{"hooks": {"PreToolUse": [{"matcher": {"tool_name": "Edit", "tool_input": {"file_path": "**/CLAUDE.md"}}, "action": "deny"}]}}` but the EXACT schema is version-specific. Check in Epic 0 spike output.
- **Fly REST API** is better-documented than flyctl for programmatic use. Use `httpx` (async) + the REST endpoints directly. Don't shell out to `flyctl` from `fly/client.py` — save that for `mars ssh` where an interactive shell is the goal.
- **Codex CLI:** OpenAI's `codex` command is new. Check exact install method and auth flow during Epic 0 spikes. If OAuth doesn't work, use API key from env var `OPENAI_API_KEY`.
- **Spike 4** requires a deployed control plane. You may need to quickly deploy mars-control to a staging environment (Fly or Vercel) during Day 5 to do the test. Scope it — 30 minutes max for staging deploy.
- **Spike 5** is literally `time mars deploy ...`. Record the output. If <30s, move on. If 30-60s, warm pool is a Day 11 add. If >60s, re-architect.
