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

## Stories (to be decomposed next cycle)

*Placeholder — next session will break this into ~5 stories:*
- Story 6.1: `runtime_local.py` local mode runner
- Story 6.2: `memory/capture.py` session history + tool log collection
- Story 6.3: `memory/sync.py` S3 periodic sync + `mars memory export`
- Story 6.4: CLAUDE.md admin edit flow (backend PATCH + supervisor reload)
- Story 6.5: `PromptEditor.tsx` web UI + `mars edit-prompt` CLI

## Notes

- **Local mode is the most important piece for Pedro's own daily workflow.** He's a Claude Code power user; local mode lets him keep using Mars in the terminal when he doesn't want to open a browser. Don't deprioritize it.
- **Immutability is a 3-layer defense:** (1) PreToolUse hook in `claude_code_settings.json` (blocks at tool dispatch time), (2) filesystem read-only bind mount (blocks at syscall time), (3) admin-only edit API (the ONLY legitimate path to update). All three layers active simultaneously.
- **Memory bundles are tarballs, not individual files in S3.** Tarball = one upload per session per sync cycle = cheaper S3 requests + easier lifecycle management.
- **"Data moat" is aspirational for v1.** v1 just captures; the moat only materializes in v1.1+ when you start feeding captures back into prompt improvements or fine-tunes. Don't over-architect the capture schema trying to prematurely optimize for the eventual training pipeline.
- **Admin = workspace owner** for v1 (single-user workspaces). When teams arrive in v2, add an explicit admin role.
- **CLAUDE.md diff proposals** are the most interesting capture type. When the model writes something like "I should add 'never assume user intent without confirmation' to my memory", that's a signal. V1 captures these; v2 builds a review UI; v3 auto-applies approved proposals.
