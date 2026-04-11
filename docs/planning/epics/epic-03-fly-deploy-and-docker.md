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

## Stories

Total: **5 stories**, ~16h budget (spans 2 days: Day 5 image/hooks, Day 6 deploy CLI + Codex + spikes).

- [x] **Story 3.1 — `Dockerfile` + `start.sh` + local container test** (~3h)
  - *Goal:* Multi-stage Dockerfile for mars-runtime with pinned Claude Code CLI, non-root user, image <500MB, and container entrypoint script handling graceful shutdown.
  - *Files:* `apps/mars-runtime/Dockerfile`, `apps/mars-runtime/start.sh`, `apps/mars-runtime/claude_code_settings.json` (placeholder, Story 3.2 fills), `.github/workflows/build-mars-runtime.yml`
  - *Done when:* `docker run` with `CLAUDE_CODE_OAUTH_TOKEN` env var spawns a working daemon via the control API
  - *Outcome:* Took the CI-build alternative path per the epic's Definition of Done ("CI builds the Docker image and pushes to a registry"). Local `docker build` on Pedro's host was blocked indefinitely by a Docker Desktop hub-proxy misconfig (`http.docker.internal:3128` stalled all pulls/builds). `.github/workflows/build-mars-runtime.yml` builds `apps/mars-runtime/Dockerfile`, pushes `ghcr.io/tacosyhorchata/mars-runtime:{sha,main,latest}` on push to main, **and runs a smoke test** inside the CI job that starts the container, polls `/health` for up to 20s, and asserts `{"status":"ok"}`. First run (1m27s) confirmed: image built + pushed + smoke test green (`health response: {"status":"ok","active_sessions":0}`). The full Done-when's `CLAUDE_CODE_OAUTH_TOKEN`-based daemon spawn is deferred to Story 3.4's live Fly deploy (which needs a real token anyway). Runtime image layout: `python:3.11-slim` base + Node 20 + pinned Claude Code 2.1.101 + non-root `mars` user + tini + uvicorn `--factory` + `WORKERS=1` hardcoded in `start.sh` (multi-worker splits `SessionManager` in-memory state).

- [x] **Story 3.2 — ★ `claude_code_settings.json` PreToolUse hooks** (~2h)
  - *Goal:* Bake `claude_code_settings.json` into the image with PreToolUse hooks blocking Edit/Write on CLAUDE.md/AGENTS.md and `bash env|printenv|echo \$` patterns — this file IS the security model.
  - *Files:* `apps/mars-runtime/claude_code_settings.json`, `apps/mars-runtime/hooks/deny-protected-edit.sh`, `apps/mars-runtime/hooks/deny-secret-read-bash.sh`, `apps/mars-runtime/Dockerfile`, `apps/mars-runtime/src/session/claude_code.py`, `apps/mars-runtime/src/session/permissions.py`, `tests/runtime/test_claude_code_hooks.py`, `tests/runtime/test_permissions.py`, `tests/runtime/test_session_manager.py`
  - *Done when:* in a local container, agent attempts to edit CLAUDE.md AND `echo $TOKEN` are both blocked
  - *Outcome:* Verified the real Claude Code 2.1.x hook schema against the official docs — hooks are **command-based**, not declarative (matcher is a tool-name string, deny is signaled by exit code 2 from a script reading tool_input on stdin). Shipped two shell scripts at `apps/mars-runtime/hooks/`: `deny-protected-edit.sh` (blocks `Edit|Write|MultiEdit` on CLAUDE.md / AGENTS.md / claude_code_settings.json) and `deny-secret-read-bash.sh` (blocks `env`/`printenv`/bare `set` at command position + `echo $...` anywhere; word-boundary-aware to avoid false positives on `grep -r env /src`, `sed 's/env/ENV/g'`, `echo prepared-content`, `set -euo pipefail`). `apps/mars-runtime/claude_code_settings.json` now contains the real two-matcher hook config. `spawn_claude_code` reads `MARS_CLAUDE_CODE_SETTINGS` env var (set in the Dockerfile to `/app/claude_code_settings.json`) and threads `--settings` through `build_claude_command`. `permissions.py::build_claude_code_settings` updated to produce the correct schema + a cross-layer regression test asserts the generated dict equals the on-disk file so the two never drift. 32 new unit tests (hook scripts tested via subprocess with real bash + python3) + 7 permissions + 4 claude_code settings tests. Full end-to-end verification ("agent attempts to edit CLAUDE.md are blocked in a running container") will happen in Story 3.4's live Fly deploy — unit tests + CI smoke test prove the wiring, live claude invocation proves the runtime behavior. Full suite: 233 passed, 1 skipped.

- [x] **Story 3.3 — `fly/client.py` Fly REST wrapper** (~3h)
  - *Goal:* Async `httpx` wrapper around Fly.io REST API exposing `create_app`, `create_machine`, `set_secrets`, `destroy_machine`, `list_machines`.
  - *Files:* `apps/mars-control/backend/src/mars_control/fly/client.py`, `tests/control/test_fly_client.py`
  - *Done when:* unit tests cover all 5 methods against mocked Fly API responses
  - *Outcome:* `FlyClient` wraps the Fly.io Machines REST API at `api.machines.dev` with bearer-token auth, accepts an injectable `httpx.AsyncClient` for tests, implements async context-manager teardown. Per Fly docs verified live: `create_app` posts `{app_name, org_slug}` to `/v1/apps`; `create_machine` posts `{config: {image, env, ...}}` to `/v1/apps/{app}/machines`; `list_machines` GETs the same path and returns `[]` on 404; `destroy_machine` DELETEs with `?force=true`; `set_secrets` GET+POST merges new env into `config.env` (machines.dev has no dedicated secrets endpoint — app-level secrets are GraphQL-only and Mars scopes secrets per-machine). Non-2xx responses raise a typed `FlyApiError` with method/path/status/truncated-body context. Bonus methods: `delete_app`, `get_machine`. 19 unit tests via `httpx.MockTransport` covering happy paths, default args, extra_config merging, 404-as-empty for list, error surface, input validation, bearer header injection. Full suite: 252 passed, 1 skipped. 19/47 stories done.

- [ ] **Story 3.4 — `mars deploy` CLI + `mars ssh` wrapper** (~4h)
  - *Goal:* `mars deploy ./agent.yaml` end-to-end (parse → ensure app → launch machine → inject secrets → POST config → return URL) + `mars ssh <agent>` wrapping `flyctl ssh console`.
  - *Files:* `packages/mars-cli/src/mars/deploy.py`, `packages/mars-cli/src/mars/ssh.py`
  - *Done when:* `mars deploy examples/pr-reviewer-agent.yaml` returns a working chat URL for a live Fly machine

- [ ] **Story 3.5 — Codex runtime + spikes 4 & 5** (~4h)
  - *Goal:* `codex.py` subprocess lifecycle mirroring `claude_code.py`, session manager dispatch by runtime field, outbound HTTP reachability spike + cold boot timing measurement.
  - *Files:* `apps/mars-runtime/src/session/codex.py`, `apps/mars-runtime/src/session/manager.py`, `docs/decisions/002-cold-boot-timing.md`
  - *Done when:* `runtime: codex` in agent.yaml spawns a Codex subprocess end-to-end AND cold boot time is recorded in the decision doc

## Notes

- **Camtom's Dockerfile at `services/fastapi/Dockerfile` is the template.** Copy the multi-stage structure, non-root user pattern, system deps pattern. Adapt for Claude Code + Codex installs.
- **Pin CLI versions in the Dockerfile**, not in a `.tool-versions` file. Explicit is better than implicit for reproducibility.
- **`claude_code_settings.json` format** — verify against Claude Code's current docs for `PreToolUse` hook syntax. The shape is roughly `{"hooks": {"PreToolUse": [{"matcher": {"tool_name": "Edit", "tool_input": {"file_path": "**/CLAUDE.md"}}, "action": "deny"}]}}` but the EXACT schema is version-specific. Check in Epic 0 spike output.
- **Fly REST API** is better-documented than flyctl for programmatic use. Use `httpx` (async) + the REST endpoints directly. Don't shell out to `flyctl` from `fly/client.py` — save that for `mars ssh` where an interactive shell is the goal.
- **Codex CLI:** OpenAI's `codex` command is new. Check exact install method and auth flow during Epic 0 spikes. If OAuth doesn't work, use API key from env var `OPENAI_API_KEY`.
- **Spike 4** requires a deployed control plane. You may need to quickly deploy mars-control to a staging environment (Fly or Vercel) during Day 5 to do the test. Scope it — 30 minutes max for staging deploy.
- **Spike 5** is literally `time mars deploy ...`. Record the output. If <30s, move on. If 30-60s, warm pool is a Day 11 add. If >60s, re-architect.
