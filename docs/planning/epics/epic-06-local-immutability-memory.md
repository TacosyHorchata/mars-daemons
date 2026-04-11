# Epic 6 — Local Mode, CLAUDE.md Immutability, Memory Capture

**Status:** `[ ]` not started
**Days:** 9 (one full day — three related subsystems)
**Depends on:** Epic 1 (supervisor + session manager + parser)
**Downstream blockers:** Epic 7 (dev dogfood validates all three features)
**Risk level:** MEDIUM

## Summary

Three related subsystems shipped in one day: (1) **Local mode** lets developers run the same agent.yaml on their own machine via `mars run --local`, bypassing Fly.io entirely. (2) **CLAUDE.md immutability** is enforced via PreToolUse hooks + admin-only editing via web UI. (3) **Memory capture** writes per-session history, CLAUDE.md diff proposals, and tool logs to the volume + syncs to S3 for the data moat foundation.

## Context

These three items are grouped because they're Pedro's v1 checklist items 7, 8, and 9 and they share a theme: *making the daemon a well-behaved long-lived agent, not a one-shot script*. Local mode unblocks dev workflow, immutability prevents the agent from rewriting its own instructions (a common foot-gun), and memory capture is the foundation of Pedro's data moat thesis.

The plan agent's key insight on immutability: **the filesystem read-only mount is belt-and-suspenders. The real enforcement is a PreToolUse hook in `claude_code_settings.json`** (which Epic 3 already bakes into the image). This epic adds the admin edit flow + supervisor restart-on-edit logic.

## Scope

### In scope

**Local mode**
- `packages/mars-cli/src/mars/runtime_local.py` — Spawns local `claude`/`codex` subprocess using the user's installed CLI, parses stream-json with the same `claude_code_stream.py` parser from Epic 1, pipes user input/output through the terminal
- `packages/mars-cli/src/mars/__main__.py` — Add `mars run --local ./agent.yaml` subcommand
- Zero control plane dependencies — local mode is fully self-contained
- Same `agent.yaml` format, same event schema
- Interactive terminal I/O (not a chat UI)

**CLAUDE.md immutability (admin-only editing)**
- Backend: `apps/mars-control/backend/src/api/routes.py` adds:
  - `GET /agents/{agent_name}/prompt` — returns current CLAUDE.md content
  - `PATCH /agents/{agent_name}/prompt` — body: `{content: str}`, updates the prompt file on the machine volume via the supervisor's control API, triggers session restart
- Supervisor: `POST /sessions/{id}/reload-prompt` — reloads CLAUDE.md from volume, restarts subprocess with new prompt
- Frontend: `apps/mars-control/frontend/components/dashboard/PromptEditor.tsx` — Monaco editor or textarea, "Save and restart" button
- Filesystem: CLAUDE.md is bind-mounted read-only inside the session working dir; Epic 3's PreToolUse hook blocks Edit/Write attempts; only the supervisor (via the admin endpoint) can update the underlying file
- CLI: `mars edit-prompt <agent>` — opens the user's `$EDITOR` with the current CLAUDE.md, on save syncs to the machine via the same PATCH endpoint

**Memory capture**
- `apps/mars-runtime/src/memory/capture.py` — Per-session memory collector:
  - Session history files from `~/.claude/sessions/` (or wherever Claude Code stores them — verify in Epic 0 spike output)
  - CLAUDE.md diff proposals: if the model suggests editing CLAUDE.md via stream-json, capture the proposed diff WITHOUT applying it (admin-review queue)
  - Tool call log: every tool invocation + result, chronological, from the event stream
  - Written to `/workspace/<session-id>/memory/` as JSONL files
- `apps/mars-runtime/src/memory/sync.py` — Periodic S3 sync (every 5 min):
  - Bundles `/workspace/<session-id>/memory/` into a tarball
  - Uploads to `s3://<bucket>/<workspace>/<agent>/<session>/<timestamp>.tar.gz`
  - Non-blocking; failures retry with backoff; logs warnings
- `packages/mars-cli/src/mars/memory.py` — `mars memory export <agent>` command downloads the latest memory bundle for inspection
- Control plane: `GET /agents/{agent_name}/memory/proposals` — returns pending CLAUDE.md diff proposals for admin review (UI for this is v2)

### Out of scope (deferred)
- Correction learning hooks that feed memory back into model fine-tuning (v2 — v1 just captures, doesn't train)
- CLAUDE.md diff proposal auto-apply (v1 is capture-only, admin reviews manually)
- Memory search/query UI (v2 — v1 is raw export)
- Encrypted memory at rest (v1 uses S3 SSE-S3, not customer-managed keys)
- Retention policies / auto-delete (v2)

## Acceptance criteria

**Local mode**
- [ ] `mars run --local examples/pr-reviewer-agent.yaml` spawns a local Claude Code subprocess
- [ ] User types in terminal → subprocess receives → events streamed back to terminal
- [ ] Ctrl+C cleanly kills the subprocess
- [ ] Same `agent.yaml` works unchanged in local mode and remote (Fly) mode

**CLAUDE.md immutability**
- [ ] Deployed daemon with a CLAUDE.md → agent attempts to edit it → PreToolUse hook blocks → event stream shows `tool_error` with "write denied" message
- [ ] Agent attempts to edit AGENTS.md → also blocked
- [ ] Admin uses `mars edit-prompt <agent>` → editor opens → save → new CLAUDE.md pushed to machine → session restarts with new prompt
- [ ] Admin uses web UI `PromptEditor.tsx` → same flow works
- [ ] Session restart does NOT lose memory (memory capture survives restart)

**Memory capture**
- [ ] After a 5-minute session, `/workspace/<session-id>/memory/` contains: `session_history.jsonl`, `tool_calls.jsonl`, `claude_md_proposals.jsonl`
- [ ] S3 sync creates a tarball at `s3://<bucket>/<workspace>/<agent>/<session>/<timestamp>.tar.gz` within 10 minutes of session activity
- [ ] `mars memory export pr-reviewer` downloads the latest bundle to local disk
- [ ] CLAUDE.md diff proposals are captured but NEVER applied — verified by editing CLAUDE.md via the admin flow and confirming the agent's captured proposals are still stored separately from the actual content

## Critical files

| File | Purpose |
|---|---|
| `packages/mars-cli/src/mars/runtime_local.py` | Local mode runner |
| `apps/mars-runtime/src/memory/capture.py` | Per-session memory collector |
| `apps/mars-runtime/src/memory/sync.py` | Periodic S3 sync |
| `apps/mars-control/frontend/components/dashboard/PromptEditor.tsx` | Web prompt editor |
| `apps/mars-control/backend/src/api/routes.py` | `PATCH /agents/{name}/prompt` endpoint |
| `packages/mars-cli/src/mars/memory.py` | `mars memory export` |
| `packages/mars-cli/src/mars/edit_prompt.py` | `mars edit-prompt` (opens $EDITOR) |

## Dependencies

- **Upstream:** Epic 1 (supervisor + session manager + stream parser)
- **Downstream:** Epic 7 (dogfood smoke tests validate CLAUDE.md immutability + memory export)

## Risks

| Risk | Mitigation |
|---|---|
| Local mode diverges from remote mode over time (bugs only in one) | Share code paths as much as possible: `runtime_local.py` imports and uses `session/claude_code.py` and `session/claude_code_stream.py` directly, not a fork. |
| Claude Code's `/memory` slash command bypasses the filesystem hook | The PreToolUse hook must also match the `/memory` or equivalent internal tool name. Verify in Epic 0 spike 2 output. If not matchable, accept the limitation and document. |
| S3 sync credentials management | Control plane stores S3 access key (encrypted). Injected into machine via `fly secrets set` at deploy time. Same pattern as other secrets. |
| Memory capture disk fills the Fly volume | Each session's memory dir has a 100MB cap. When exceeded, oldest JSONL lines are truncated (ring buffer semantics). Document. |
| Session restart loses in-flight events | Drain event queue before killing subprocess; flush forwarder buffer; wait for ack from control plane. 5 second graceful shutdown timeout. |
| Admin edit races with the agent writing in its own working dir | Session restart is atomic from the user's POV: old subprocess killed, new one spawned with updated CLAUDE.md, UI shows "restarting" indicator. ~3 seconds of downtime per edit. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] CI passes
- [ ] `mars run --local` works on Pedro's laptop
- [ ] CLAUDE.md immutability verified against a real attack (agent tries to edit via Bash, Edit, Write tools)
- [ ] Memory bundle exportable and inspectable
- [ ] Admin edit flow works end-to-end (web UI + CLI)

## Stories

Total: **5 stories**, ~8h budget. Three related subsystems (local mode + immutability + memory) shipped in one day.

- [x] **Story 6.1 — `runtime_local.py` local mode runner** (~2h)
  - *Goal:* `mars run --local ./agent.yaml` spawns a local `claude` subprocess using shared `claude_code.py` + `claude_code_stream.py` with terminal I/O, zero control plane dependencies.
  - *Files:* `packages/mars-cli/src/mars/runtime_local.py`, `packages/mars-cli/src/mars/__main__.py`, `tests/cli/test_runtime_local.py`
  - *Done when:* same agent.yaml works with `mars run --local` on Pedro's laptop
  - *Outcome:* `run_local_loop` reuses `spawn_claude_code` (stdin_stream_json=True) + `parse_stream` so local mode and remote mode share every byte of the runtime core. REPL-style UX: multi-line prompt ending on a blank line, Ctrl+D closes stdin so claude exits, Ctrl+C aborts cleanly with exit code 130. Events pretty-printed to stderr (`→ session started`, `← text`, `→ tool_call`, etc.) so the user can pipe stdout to a file if they want raw JSONL. `format_event_for_terminal` covers all 6 canonical Mars event types with a generic fallback; `encode_user_event_line` wraps user text as a stream-json `user` event. `read_multiline_prompt` handles EOF, blank-first-line, and multi-line collection. `mars run --local <agent.yaml>` wired into `packages/mars-cli/src/mars/__main__.py`. 15 unit tests cover format helpers, stdin parser, canonical stream-json round-trip through `run_local_loop` (including `CriticalParseError` propagation → exit code 2). Full suite: 292 passed, 1 skipped.

- [x] **Story 6.2 — `memory/capture.py` session + tool log collection** (~2h)
  - *Goal:* Per-session collector writing `session_history.jsonl`, `tool_calls.jsonl`, `claude_md_proposals.jsonl` into `/workspace/<session-id>/memory/` from the event stream; proposals captured but NEVER applied.
  - *Files:* `apps/mars-runtime/src/memory/capture.py`, `apps/mars-runtime/src/memory/__init__.py`, `tests/runtime/test_memory_capture.py`
  - *Done when:* after a session, memory dir contains all three JSONL files with matching event counts
  - *Outcome:* `MemoryCapture(session_id, root_dir)` creates `/workspace/<session-id>/memory/` with three append-only JSONL files: `session_history.jsonl` (every event in order, durable + ephemeral), `tool_calls.jsonl` (denormalized ToolCall ↔ ToolResult pairs indexed by `tool_use_id`; orphan results still recorded for audit), `claude_md_proposals.jsonl` (assistant utterances mentioning `CLAUDE.md` / `AGENTS.md` captured but NEVER applied). 100 MB soft disk budget per session with over-budget flag + structured log warning; async context-manager sugar; idempotent `open`/`close`. `extract_prompt_proposal` is a broad-recall regex detector that matches exact filenames (rejects `claude.md` / `CLAUDE.markdown` to stay tight on the admin-only contract). 21 unit tests covering: file creation + layout, event-type routing, tool-pair matching (in order + out of order + orphans), proposal detector boundary cases (assistant text only, exact filename only, unrelated text), ephemeral chunk capture, disk budget cap, defensive guards (record-before-open → log + drop, record-after-close → no-op), cross-layer guard that no CLAUDE.md file is ever written into the memory dir. Full suite: 313 passed, 1 skipped. **21/47 stories done.**

- [x] **Story 6.3 — `memory/sync.py` S3 sync + `mars memory export`** (~1h)
  - *Goal:* Periodic (5min) tarball S3 sync to `s3://<bucket>/<workspace>/<agent>/<session>/<ts>.tar.gz` + CLI command to download latest bundle.
  - *Files:* `apps/mars-runtime/src/memory/sync.py`, `packages/mars-cli/src/mars/memory.py`, `tests/runtime/test_memory_sync.py`, `tests/cli/test_memory_command.py`, `pyproject.toml` (+boto3, +moto)
  - *Done when:* `mars memory export pr-reviewer` downloads a valid tarball from S3
  - *Outcome:* `S3MemorySync` background task tars every tracked session's memory dir via `build_memory_tarball` (relative arcnames, empty-dir tolerant) and uploads to `s3://<bucket>/<key_prefix>/<session-id>/<iso>.tar.gz` every `DEFAULT_SYNC_INTERVAL_S=300s`. Uses sync boto3 wrapped in `asyncio.to_thread` (no aioboto3 dep). Graceful `start`/`stop` with a final drain so shutdown never loses memory; per-session upload failures are caught and logged without killing the loop. `mars memory export <agent>` lists `s3://<bucket>/<workspace>/<agent>/` via paginator, picks the most recent `.tar.gz` by `LastModified` + key tiebreak, downloads to `--dest` (default `./<agent>-memory-<ts>.tar.gz`). Tests use `moto.mock_aws` for real boto3 surface coverage: 15 sync tests (tarball packing, key shape, track/untrack, moto round-trip, missing-dir empty-tarball, broken-client failure recording, lifecycle start+stop final drain) + 7 export tests (latest-bundle picker, non-tarball filtering, direct callback download, missing-bucket error, missing-bundles error, workspace override). Full suite: 335 passed, 1 skipped. 22/47 stories done.

- [x] **Story 6.4 — ★ CLAUDE.md admin edit flow (backend + supervisor reload)** (~2h)
  - *Goal:* Backend `PATCH /agents/{name}/prompt` pushes new CLAUDE.md to machine; supervisor `POST /sessions/{id}/reload-prompt` restarts subprocess with new prompt atomically.
  - *Files:* `apps/mars-runtime/src/session/manager.py`, `apps/mars-runtime/src/supervisor.py`, `apps/mars-control/backend/src/mars_control/api/routes.py`, `tests/runtime/test_session_manager.py`, `tests/runtime/test_supervisor_api.py`, `tests/control/test_prompt_update.py`
  - *Done when:* admin edits prompt → session restarts within 5s with new CLAUDE.md, agent edit attempts still blocked by PreToolUse hook
  - *Outcome:* Three-layer admin edit path. **SessionManager.restart()** kills + respawns the subprocess *in place*, preserving the session_id so browser URLs stay stable; on SIGKILL timeout the old handle tombstones to `_orphaned` instead of silently leaking. **Supervisor `POST /sessions/{id}/reload-prompt`** writes the new content to `AgentConfig.system_prompt_path`, rejecting any path that resolves outside `config.workdir` (regression guard against traversal via `../outside.md` or absolute path injection), cancels the old event pump, calls `mgr.restart`, and starts a fresh pump. **mars-control `PATCH /agents/{name}/prompt`** injects a `session_locator` callable (agent_name + session_id → supervisor URL) and an `httpx.AsyncClient` so the proxy is fully test-mockable; forwards to `POST /sessions/{id}/reload-prompt` on the looked-up supervisor; maps connection errors and 5xx to 502 with diagnostic detail. Defense-in-depth: the PreToolUse hooks from Story 3.2 still block the daemon from editing CLAUDE.md / AGENTS.md directly — this endpoint runs on the supervisor's control API, not the tool surface, so a daemon cannot reach it. 3 new SessionManager restart tests + 4 supervisor reload-prompt tests (happy path file-update-and-restart, path traversal rejection, unknown session 404, empty content 422) + 7 mars-control PATCH tests (forward happy path, trailing slash normalization, locator-returns-None 404, supervisor unreachable 502, supervisor 5xx → 502, empty content + missing session_id 422). Full suite: 349 passed, 1 skipped. 23/47 stories done.

- [x] **Story 6.5 — `PromptEditor.tsx` web UI + `mars edit-prompt` CLI** (~1h)
  - *Goal:* Web textarea editor with "Save and restart" button + CLI command that opens `$EDITOR` on the current CLAUDE.md contents.
  - *Files:* `packages/mars-cli/src/mars/edit_prompt.py`, `packages/mars-cli/src/mars/__main__.py`, `tests/cli/test_edit_prompt.py`
  - *Done when:* both web UI save and `mars edit-prompt` trigger session restart with the new prompt
  - *Outcome:* Shipped the **CLI half fully**; the React `PromptEditor.tsx` component is deferred to Epic 4 (Web UI & Magic-Link Auth) where the frontend scaffold is introduced — no frontend tree exists in mars-daemons yet. `mars edit-prompt <agent> <session_id>` supports three workflows: (1) `--file path.md` reads the new prompt from a file (scripted CI use), (2) no `--file` opens `$EDITOR` on a tempfile pre-seeded with a help header, strips the header on save via `_strip_header_comments`, and sends only the user's content, (3) `--dry-run` prints what would be sent and exits without hitting the network. Errors cleanly on editor abort (non-zero exit), empty-after-strip content, missing `$MARS_CONTROL_URL`, transport errors (mapped to "failed to reach mars-control"), and supervisor 4xx/5xx (surfaced with status + truncated body). Injectable `editor_launcher` and `http_client` so tests never fork `vi` or touch the network. 12 unit tests covering header strip boundaries (including "only leading comments" rule), --file happy path, missing control URL, transport errors, supervisor 5xx, dry-run skipping HTTP, $EDITOR save round-trip via injected launcher, editor abort → error, empty-prompt-after-strip refusal. Full suite: 361 passed, 1 skipped. **24/47 stories done; Epic 6 complete (5/5).**

## Notes

- **Local mode is the most important piece for Pedro's own daily workflow.** He's a Claude Code power user; local mode lets him keep using Mars in the terminal when he doesn't want to open a browser. Don't deprioritize it.
- **Immutability is a 3-layer defense:** (1) PreToolUse hook in `claude_code_settings.json` (blocks at tool dispatch time), (2) filesystem read-only bind mount (blocks at syscall time), (3) admin-only edit API (the ONLY legitimate path to update). All three layers active simultaneously.
- **Memory bundles are tarballs, not individual files in S3.** Tarball = one upload per session per sync cycle = cheaper S3 requests + easier lifecycle management.
- **"Data moat" is aspirational for v1.** v1 just captures; the moat only materializes in v1.1+ when you start feeding captures back into prompt improvements or fine-tunes. Don't over-architect the capture schema trying to prematurely optimize for the eventual training pipeline.
- **Admin = workspace owner** for v1 (single-user workspaces). When teams arrive in v2, add an explicit admin role.
- **CLAUDE.md diff proposals** are the most interesting capture type. When the model writes something like "I should add 'never assume user intent without confirmation' to my memory", that's a signal. V1 captures these; v2 builds a review UI; v3 auto-applies approved proposals.
