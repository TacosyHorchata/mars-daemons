# Mars v1 — Implementation Plan

**Status:** FINAL — Pedro's decisions integrated, approved 2026-04-10
**Repo:** `github.com/tacosyhorchata/mars-daemons` (new standalone repo, Pedro's chosen name — reinforces the Mars/daemons naming hierarchy)
**Target:** Pedro's 10-item v1 checklist + Maat turnkey template · Design partner: Maat (Orion) · Operator wedge first
**Ship target:** **10-day build with spikes in parallel** + 3-day buffer = **13 days total**
**Auth level:** Magic-link public email signup (Resend or similar) — adds ~0.5-1 day, enables discovery from day 1

**Decisions locked (2026-04-10):**
1. ✅ Both tracks ship in v1: developer (agent.yaml + CLI) AND operator (Maat turnkey template)
2. ✅ Commit 10-day build; spikes run in parallel inside Day 1-2 (riskier but faster in nominal case)
3. ✅ New standalone repo `mars-daemons` (not subfolder of camtom-platform)
4. ✅ Magic-link email signup for v1 (public, not manual provisioning)

---

## Context

Mars is Pedro's second-company bet: a cloud platform for hosting AI agents ("daemons") powered by existing CLI runtimes (Claude Code + OpenAI Codex) rather than a custom agent loop. Pedro charges for hosting + orchestration only — BYOLLM, zero token markup, ride the Claude Max / ChatGPT Plus subscription subsidy.

**Why now / intended outcome:**
- **Design Partner #1:** Maat (CEO de Orion, rastreadores Mérida/Celaya, ~$15K MRR) validated the product with eyes-lit-up reaction on 2026-04-10. Does NOT know Claude Code — wants outcomes in a chat, not runtimes in a terminal. **Maat's path cannot require writing YAML or using a CLI.**
- **Window:** 12–18 months before Anthropic ships "Claude Code for Teams" or restricts headless OAuth. Mars = operator-shell + multi-runtime; Anthropic's version will be dev-focused.
- **Leverage:** Pedro has validated patterns in Camtom (event-sourced loop, EventSink Protocol with HTTP + SSE + Redis impls, multi-stage Dockerfile, FastAPI SSE with heartbeat). Mars reuses these almost verbatim.
- **v1 goals:**
  1. Pedro dogfoods Mars with a PR-reviewer daemon on `epic/agents-v2` — developer track
  2. Maat uses Mars from his phone via a pre-baked "Tracker Ops Assistant" template — operator track
  3. Ship both in 13 days (10-day build with spikes in parallel inside Day 1-2 + 3 days for Maat template + security docs + Maat onboarding call)

---

## v1 Scope

**Pedro's 10-item checklist (developer track):**
- [ ] `agent.yaml` — declarative daemon config format
- [ ] Deploy daemon to Fly.io via `mars deploy`
- [ ] SSH access to Fly VM (reuse `fly ssh console`, no custom code)
- [ ] OAuth connection: Claude Code + Codex against user's own accounts
- [ ] Native chat with Claude Code / Codex (via `claude -p --output-format stream-json` parser)
- [ ] Multi-session per Fly VM; UI lists active sessions by name + description
- [ ] Local mode — `mars run --local` spawns subprocess with terminal I/O
- [ ] CLAUDE.md / AGENTS.md modifiable ONLY by admin (platform UI or local CLI), never by agent
- [ ] Memory storage per runtime protocol (data moat foundation)
- [ ] Sandbox secret injection (env var + PreToolUse hook speed bump, documented limitation)

**Operator track (Maat turnkey — APPROVED):**
- [ ] One pre-baked template `tracker-ops-assistant` (pre-written agent.yaml, pre-wired MCPs: WhatsApp, Zoho, Pilot browser)
- [ ] Onboarding flow: magic-link signup → "Start Tracker Ops Assistant" button → OAuth Anthropic → chat opens. Zero YAML, zero CLI.
- [ ] Maat never sees the agent.yaml abstraction. It's there, but hidden behind the template.

**Auth track (public magic-link signup — APPROVED):**
- [ ] Magic-link email signup via Resend (or similar transactional provider)
- [ ] JWT session cookie after magic link click
- [ ] Protected routes redirect to signup
- [ ] Single-user workspace model for v1 (no teams, no RBAC)

---

## Key exploration findings

### Patterns to reuse from Camtom (`services/fastapi/src/products/agents/`)

| Pattern | Source | Mars destination |
|---|---|---|
| **Event types (durable + ephemeral split)** | `agent/events.py:18-111` — `DURABLE_EVENTS` save→emit, `EPHEMERAL_EVENTS` fire-and-forget | Mirror at `mars-runtime/src/events/types.py` |
| **EventSink Protocol + SSEEventSink + HttpEventSink** | `agent/sink.py:27-322` — Protocol at 27-30; `SSEEventSink` lines 85-140; `HttpEventSink` lines 33-83 | **Mars flips the topology:** machine uses `HttpEventSink` to POST events outbound to control plane; control plane uses `SSEEventSink` to hold browser connections. Single SSE hop. |
| **SSE format, heartbeat, idle timeout** | `router.py:322-329` (`_format_sse_event`), lines 52-53 (`_SSE_HEARTBEAT_INTERVAL=30s`, `_SSE_IDLE_TIMEOUT=5min`), lines 999-1128 (generator) | Lift verbatim to `mars-control/backend/src/sse/stream.py` |
| **Dockerfile** | `services/fastapi/Dockerfile` — multi-stage, Python 3.11-slim, non-root, env-var secrets | Template for both `mars-control/backend/Dockerfile` and `mars-runtime/Dockerfile` |
| **Confirmation/permission prompts** | `agent/loop.py:692-739` — `confirmation_required` event + round-trip pattern | Mirror for Claude Code permission prompts in `mars-runtime/src/session/permissions.py` |
| **AuthProvider Protocol** | `agent/auth.py:10-95` — JWT Bearer, pluggable | Template for Mars control plane user auth |

### Critical findings — unknowns that must be validated Day 1-2

**Claude Code headless OAuth is NOT implemented in Camtom.** The `reference_claude_oauth_auth.md` memory refers to knowledge outside this repo. Mars must prove it works end-to-end before committing to architecture.

**There is no stable "Claude Code SDK" for Python.** The correct integration path is `claude -p --output-format stream-json` (JSONL events) — NOT a programmatic SDK. The event schema is undocumented and version-pinned. This MUST be contract-tested in CI with the pinned CLI version.

---

## Architecture — two-plane split with flipped SSE topology

### Control plane — `mars-control` (Next.js + FastAPI, multi-tenant SaaS)
- **Frontend:** Next.js + Vercel AI SDK `useChat` hook. Dashboard, chat, prompt editor, template launcher.
- **Backend:** FastAPI. Routes for auth, workspace CRUD, agent.yaml upload, Fly.io machine orchestration, **inbound event ingestion from machines** (POST), **outbound SSE to browsers** (using Camtom's `SSEEventSink` fan-out).
- **Storage:** SQLite for v0.1. Tables: `users`, `workspaces`, `agents`, `sessions`, `oauth_tokens` (Fernet-encrypted), `secrets` (Fernet-encrypted), `events` (durable log for SSE replay).
- **Object store:** S3 or Fly volume for session memory artifacts (keyed by `workspace/agent/session/timestamp`).

### Data plane — `mars-runtime` (one Fly machine per workspace)
- **Container:** Python 3.11-slim + pinned `@anthropic/claude-code` CLI + pinned OpenAI Codex CLI.
- **Supervisor:** FastAPI app that:
  - Exposes control API: `POST /sessions` (spawn), `GET /sessions` (list), `DELETE /sessions/:id` (kill), `POST /sessions/:id/input` (send user message), `POST /sessions/:id/permission-response` (respond to tool-approval prompt)
  - Spawns Claude Code via `asyncio.create_subprocess_exec(['claude', '-p', prompt, '--output-format', 'stream-json', '--append-system-prompt', ...])` per session
  - Parses stream-json → Mars event schema (see `session/claude_code_stream.py`)
  - **Uses `HttpEventSink` to POST events outbound to control plane** (machine does NOT hold inbound SSE connections)
  - Maintains `active_sessions` dict with persistent handle on volume (`/workspace/<session-id>/supervisor_handle.json`) for crash recovery
  - Reconciles active_sessions against volume on startup (`supervisor_recovery.py`)
- **Volume:** One Fly volume per machine at `/workspace`. Subdir per session: `/workspace/<session-id>/`. Session working files + read-only CLAUDE.md bind-mount + session history.
- **SSH:** Reuse `fly ssh console` — zero custom code. `mars ssh <agent>` wraps flyctl.
- **Secrets:** Injected via `fly secrets set` → env vars available to supervisor → passed through to subprocess env with secret-name prefix filter.
- **`claude_code_settings.json` baked into image** — contains `PreToolUse` hooks that block `Edit`/`Write` targeting CLAUDE.md/AGENTS.md, and block `bash` commands matching `env|printenv|echo\s+\$`. **This file IS the security model, not a config detail.**

---

## Per-item implementation

| # | Item | Implementation | Risk |
|---|---|---|---|
| 1 | `agent.yaml` | Single Pydantic `AgentConfig` class (no Protocol). Fields: `name`, `description`, `runtime`, `system_prompt_path`, `mcps[]`, `env[]`, `tools[]`, `workdir`. Parser in `mars-control/backend/src/schema/agent.py`. | Low |
| 2 | Fly deploy | `mars deploy ./agent.yaml`. Python Click CLI wraps Fly.io REST API: create app → create machine → set secrets → POST agent.yaml to supervisor → return URL. | Low |
| 3 | SSH | `mars ssh <agent>` wraps `flyctl ssh console -a <app>`. Zero custom code. | Low |
| 4 | OAuth CC + Codex | **Day 1 spike blocks commitment.** Path A (if headless works): `claude setup-token` → capture `CLAUDE_CODE_OAUTH_TOKEN` → control plane stores encrypted → inject at machine deploy. Codex: same shape OR API-key fallback. Path B (if headless fails): BYO-API-key for both, adjust marketing. | **HIGH — must validate Day 1** |
| 5 | Native chat | **NOT a "lift Camtom EventSink" job.** Build `session/claude_code_stream.py` — a JSONL parser that translates `stream-json` events (`system_init`, `assistant`, `user` tool_result, `result`) into Mars event schema. Contract-tested in CI (`tests/contract/test_claude_code_stream.py`) with pinned Claude Code version. Frontend renders 4 component types: `assistant_text`, `tool_call`, `tool_result`, `permission_request`. **Budget 3 days for this file alone.** | **HIGH — highest-risk file in project** |
| 6 | Multi-session per VM | In-memory `active_sessions` dict + per-session handle persisted to volume. Reconciliation on supervisor startup (`supervisor_recovery.py`) scans volume, marks orphan sessions as "needs restart". No auto-resume (prevents double-runs). Hard cap 3 concurrent sessions per machine in v1. | Medium — covered by recovery logic |
| 7 | Local mode | `mars run --local ./agent.yaml`. Same CLI. Spawns local `claude -p` subprocess with same stream-json parser. Terminal I/O via stdin/stdout. Zero control plane dependency. File: `mars-cli/src/mars/runtime_local.py`. | Low |
| 8 | CLAUDE.md immutability | **Real enforcement = `PreToolUse` hook in `claude_code_settings.json`** that blocks Edit/Write targeting `CLAUDE.md` and `AGENTS.md`. Filesystem read-only mount is belt-and-suspenders. Admin edits via web UI prompt editor → pushes to control plane → supervisor restarts session with new prompt. CLI: `mars ssh` + manual file edit for devs. | Medium — requires hook is respected |
| 9 | Memory capture | Supervisor captures per session: `~/.claude/sessions/*` files, CLAUDE.md diff proposals (captured from stream-json, NEVER applied — admin-review queue), tool call log from event stream. Writes to `/workspace/<session-id>/memory/` → periodic sync to S3. API: `mars memory export <agent>`. | Low |
| 10 | Secrets | Control plane encrypts via Fernet. `fly secrets set` at deploy. Env vars in supervisor → filtered subprocess env. **PreToolUse hook** blocks `bash` commands matching `env|printenv|echo\s+\$` as speed bump. **`docs/security.md` states exact threat model explicitly.** | Low-Medium with docs |

### Operator track — Maat turnkey template

Without this, Maat cannot use Mars and the design-partner thesis collapses.

- **File:** `apps/mars-control/templates/tracker-ops-assistant.yaml` (pre-baked agent.yaml)
- **Content:** pre-wired MCPs (WhatsApp, Zoho, Pilot browser), generic system prompt tuned for "ops assistant for a tracker company", placeholder secrets the onboarding flow fills in.
- **UX:** Dashboard has a "Templates" tab. Click "Tracker Ops Assistant" → onboarding wizard asks for Claude Max subscription → walks Anthropic OAuth → asks for Zoho API key → deploys the daemon → opens chat. **Maat never sees agent.yaml.**
- **Under the hood:** Same mars-runtime, same supervisor, same everything. Template is just a pre-filled agent.yaml + an onboarding UI override.

---

## Repo structure — `github.com/tacosyhorchata/mars-daemons`

```
mars-daemons/
├── apps/
│   ├── mars-control/
│   │   ├── backend/
│   │   │   ├── src/
│   │   │   │   ├── schema/agent.py                 # Pydantic AgentConfig (concrete, no Protocol)
│   │   │   │   ├── fly/client.py                   # Fly.io REST wrapper
│   │   │   │   ├── oauth/providers.py              # Both Anthropic + OpenAI in one file for v1
│   │   │   │   ├── store/session.py                # Concrete SQLite class (no Protocol yet)
│   │   │   │   ├── sse/stream.py                   # Lifted from Camtom SSE pattern
│   │   │   │   ├── events/ingest.py                # HTTP endpoint receiving events from machines
│   │   │   │   ├── sessions/reconcile.py           # Cross-check machine vs control plane state
│   │   │   │   └── api/routes.py
│   │   │   └── Dockerfile                          # Camtom multi-stage template
│   │   ├── frontend/
│   │   │   ├── app/                                # Next.js app router
│   │   │   ├── components/chat/                    # 4 components: text, tool_call, tool_result, permission_request
│   │   │   ├── components/dashboard/               # Session list, prompt editor
│   │   │   └── components/templates/               # Template launcher (Maat turnkey UX)
│   │   └── templates/
│   │       └── tracker-ops-assistant.yaml          # Maat's pre-baked agent
│   └── mars-runtime/
│       ├── src/
│       │   ├── supervisor.py                       # FastAPI control API
│       │   ├── supervisor_recovery.py              # Startup reconciliation from volume
│       │   ├── session/manager.py                  # Spawn/list/kill
│       │   ├── session/claude_code.py              # CC subprocess lifecycle
│       │   ├── session/claude_code_stream.py       # ★ stream-json parser — highest-risk file
│       │   ├── session/codex.py                    # Codex subprocess lifecycle
│       │   ├── session/permissions.py              # Permission prompt round-trip
│       │   ├── events/forwarder.py                 # NOT a sink — forwards to control plane via HTTP
│       │   └── events/types.py                     # Durable/ephemeral event types (mirrors Camtom)
│       ├── claude_code_settings.json               # ★ PreToolUse hooks — this IS the security model
│       └── Dockerfile                              # Python 3.11-slim + pinned claude + pinned codex
├── packages/
│   └── mars-cli/
│       └── src/mars/
│           ├── __main__.py
│           ├── init.py                             # `mars init` — scaffold agent.yaml in a repo
│           ├── deploy.py
│           ├── ssh.py
│           ├── runtime_local.py                    # Local mode (spawns local CC/Codex)
│           └── runtime_remote.py                   # Remote mode (deploys to Fly)
├── tests/
│   └── contract/
│       └── test_claude_code_stream.py              # ★ pins CC version, asserts event schema
├── docs/
│   ├── agent-yaml-spec.md
│   ├── getting-started.md
│   ├── oauth-setup.md
│   └── security.md                                 # ★ v1 threat model — write BEFORE launch
└── examples/
    ├── pr-reviewer-agent.yaml                      # Pedro's dogfood
    └── orion-daemon.yaml                           # Template reference
```

★ = critical files identified by plan agent stress-test.

---

## Parallel spike strategy (inside Day 1-2)

Pedro chose parallel spikes over a dedicated Day 0. Spikes 1-3 run alongside foundation work. The architecture is designed so that if any spike fails, the pivot is bounded — you lose hours, not days.

### Spikes that must finish inside Day 1-2 (hard gate before Day 3)

1. **Claude Code headless OAuth spike (2h, Day 1 AM).** `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` → run `claude -p "hi" --output-format stream-json` in a clean Docker container (not a Fly VM yet, just local Docker). Validate: browser-free? token lifetime? works in Docker? Same test for Codex.
   - **Failure pivot:** BYO-API-key mode. Architecture unchanged, marketing adjusted. No day lost.
2. **stream-json schema capture (1h, Day 1 AM).** `claude -p "edit README.md to add a line, then run ls" --output-format stream-json` → dump every event → write contract test fixture → classify events. This becomes the canonical fixture for `tests/contract/test_claude_code_stream.py` and the input to Day 3 work on `session/claude_code_stream.py`. **Must complete before Day 3 or Day 3 is building on an unknown schema.**
3. **Permission prompt round-trip (2h, Day 2 AM).** Tool requires approval → can the prompt be intercepted in stream-json? Can a decision be injected back (stdin, `--permission-mode`, or `settings.json` allowlist)? Note exact mechanism.
   - **Failure pivot:** `--permission-mode acceptEdits` + denylist hook. Means v1 has limited human-in-loop, but ships.

### Spikes that can run during regular work (Day 5-6)

4. **Fly machine outbound HTTP reachability (1h, Day 5).** During Fly deploy day, validate: machine can POST to `mars-control` public URL with signed `X-Event-Secret` header. TLS end-to-end. This is the SSE topology flip's technical prerequisite.
5. **Fly machine cold boot + volume mount timing (30min, Day 5).** Pure measurement. Wall-clock from `mars deploy` to first session ready. >30s → need warm-pool strategy for Maat (document, add to Day 11).

**Hard rule:** if spike 1 or 2 fails on Day 1, **stop and re-plan before starting Day 3**. Spike 2 output is a hard prerequisite for the highest-risk file (`claude_code_stream.py`).

---

## Day-by-day execution (13 days)

- **Day 1 AM** — Spikes 1 + 2 (CC OAuth, stream-json schema capture). **Day 1 PM** — `agent.yaml` Pydantic schema + parser + 2 example files (pr-reviewer, orion) + `mars init` command. All in `mars-control/backend/src/schema/agent.py` and `packages/mars-cli/src/mars/init.py`.
- **Day 2 AM** — Spike 3 (permission prompt round-trip). **Day 2 PM** — `mars-runtime` supervisor skeleton: FastAPI control API at `apps/mars-runtime/src/supervisor.py`, `POST /sessions`, in-memory session dict, subprocess spawn (Claude Code only, Codex added Day 6).
- **Day 3** — ★ **`apps/mars-runtime/src/session/claude_code_stream.py`** — JSONL parser driven by Day 1 spike fixture, Mars event schema mapping, contract test in `tests/contract/test_claude_code_stream.py`. **Highest-risk file in project.** If spike 2 failed, redesign first.
- **Day 4** — Event forwarding topology: `apps/mars-runtime/src/events/forwarder.py` (outbound HTTP from machine) + `apps/mars-control/backend/src/events/ingest.py` (receive machine POSTs with `X-Event-Secret`) + `apps/mars-control/backend/src/sse/stream.py` (browser SSE fanout, lifted verbatim from Camtom `router.py:322-329,999-1128`).
- **Day 5** — `apps/mars-runtime/Dockerfile` (Camtom multi-stage template + pinned `claude` CLI + pinned `codex` CLI + `claude_code_settings.json` with PreToolUse hooks blocking CLAUDE.md/AGENTS.md edits + `env`/`printenv`/`echo $` patterns). Build image. Run container locally with test OAuth token. **Spike 4 + 5 during Fly test deploy.**
- **Day 6** — Fly.io deploy end-to-end: `mars deploy` CLI (`packages/mars-cli/src/mars/deploy.py`) → control plane creates Fly app → launches machine from mars-runtime image → `fly secrets set` → POST agent.yaml to supervisor → returns URL. First remote daemon live. Codex subprocess added as parallel runtime in `session/codex.py`.
- **Day 7** — Web chat UI part 1: Next.js + Vercel AI SDK `useChat` → 4 component types (`assistant_text`, `tool_call`, `tool_result`, `permission_request`). Dashboard with session list (name + description from agent.yaml). **Magic-link auth (Resend) integrated** — minimal flow: email → magic link → JWT session cookie.
- **Day 8** — Multi-session: concurrent sessions on one VM, `apps/mars-runtime/src/supervisor_recovery.py` (volume scan on startup, mark orphans as "needs restart" without auto-resume), hard cap 3 sessions/VM, per-session log routing with `session_id` tag. `apps/mars-control/backend/src/sessions/reconcile.py` cross-checks machine vs control plane state on reconnect.
- **Day 9** — Local mode: `packages/mars-cli/src/mars/runtime_local.py` (spawns local `claude`/`codex` subprocess, stdin/stdout pass-through, same agent.yaml). CLAUDE.md immutability end-to-end: web UI prompt editor in `apps/mars-control/frontend/components/dashboard/PromptEditor.tsx` → PATCH endpoint → supervisor restart-on-edit flow. Memory capture skeleton writes to `/workspace/<session-id>/memory/` + periodic S3 sync.
- **Day 10** — Developer-track dogfood: Pedro deploys PR reviewer on `epic/agents-v2`. End-to-end smoke tests (see Verification plan).
- **Day 11** — **Operator track:** `apps/mars-control/templates/tracker-ops-assistant.yaml` (pre-baked agent with WhatsApp + Zoho + Pilot MCPs, generic ops-assistant system prompt). Dashboard "Templates" tab in `apps/mars-control/frontend/components/templates/TemplateLauncher.tsx`. Onboarding wizard: OAuth Anthropic → Claude Max verification → Zoho API key input → deploy → chat opens. Maat never sees YAML or CLI.
- **Day 12** — `docs/security.md` (explicit v1 threat model) + secrets hardening (PreToolUse hook refinement, audit env var exposure) + bug fixes from Day 10-11 dogfood.
- **Day 13** — Maat setup call: Pedro walks Maat through the template onboarding live (screen share). Capture Maat's reactions and first-week feedback items as v1.1 backlog. **Ship.**

---

## Risks + mitigations

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Claude Code headless OAuth broken in containers** (TOS, browser flow, token refresh, rate-limit pool) | **CRITICAL** | Day 1 spike #1. Fail → pivot to API-key mode same architecture. |
| 2 | **stream-json schema version drift** (Anthropic changes CLI output silently) | **HIGH** | Pin `claude` CLI version in Dockerfile. Contract test in CI (`tests/contract/test_claude_code_stream.py`). Breaks in CI before production. |
| 3 | **Maat cannot use YAML + CLI product** — design partner thesis collapses | **HIGH — product risk** | Turnkey template as first-class v1 deliverable (Day 11). Without it, v1 is founder-only and Maat is a vanity metric. |
| 4 | **Permission prompt round-trip is gnarly** (stdin injection, TTY expectations) | MEDIUM-HIGH | Day 2 spike #3. Fallback: `--permission-mode acceptEdits` + denylist hook. Means v1 has limited human-in-loop but ships. |
| 5 | **CLAUDE.md immutability bypassed via Claude Code `/memory` command or internal edit tools** | MEDIUM | Real enforcement = `PreToolUse` hook in `claude_code_settings.json` (blocks Edit/Write on CLAUDE.md + AGENTS.md). Filesystem read-only = belt. Test during Day 9. |
| 6 | **Two-hop SSE reconnect bugs** (mars-control relays mars-runtime SSE) | MEDIUM | **Eliminated by flipping topology** — machine POSTs events outbound to control plane; control plane holds browser SSE. Reuses Camtom `HttpEventSink` + `SSEEventSink` verbatim. |
| 7 | **Multi-session state corruption on machine restart** (in-memory dict lost) | MEDIUM | `supervisor_recovery.py` scans volume on startup, marks orphans as "needs restart" in control plane, no auto-resume (prevents double-runs). Persistent session handle on volume. |
| 8 | **Agent reads secrets via `echo $API_KEY`** | MEDIUM | v1: PreToolUse hook blocks `env`/`printenv`/`echo $` patterns as speed bump. `docs/security.md` states threat model honestly: "code you wrote, keys you own, us hosting the VM". v2: outbound HTTP proxy with secret substitution. |
| 9 | **Anthropic ships Claude Code for Teams during v1** | Low-Medium | Positioning (operator-shell + multi-runtime) stays differentiated. Anthropic's version will be dev-focused single-runtime. |
| 10 | **Fly machine cold boot >30s breaks Maat onboarding** | Medium | Day 5 spike #5 measures. If slow, adopt warm-pool strategy: keep 1 machine per template pre-warmed. |

---

## Critical files for implementation

| File | Purpose | Source pattern |
|---|---|---|
| `apps/mars-runtime/src/session/claude_code_stream.py` | JSONL parser: stream-json → Mars events. **Highest-risk file.** | Build from Day 1 spike fixture |
| `apps/mars-runtime/claude_code_settings.json` | PreToolUse hooks enforcing CLAUDE.md immutability + secret-read blocks. **THE security model.** | Claude Code docs |
| `apps/mars-runtime/src/events/forwarder.py` | Outbound HTTP event forwarding (not a sink) | Lifts `HttpEventSink` from `services/fastapi/src/products/agents/agent/sink.py:33-83` |
| `apps/mars-control/backend/src/events/ingest.py` | HTTP endpoint receiving machine events | New — validates `X-Event-Secret` |
| `apps/mars-control/backend/src/sse/stream.py` | Browser SSE fanout | Lifts `SSEEventSink` + `_format_sse_event` + heartbeat constants verbatim from `services/fastapi/src/products/agents/agent/sink.py:85-140` and `router.py:322-329,52-53,999-1128` |
| `apps/mars-runtime/src/session/permissions.py` | Permission prompt round-trip | Mirrors Camtom `confirmation_required` pattern at `agent/loop.py:692-739` |
| `apps/mars-runtime/src/supervisor_recovery.py` | Volume-based session reconciliation on startup | New |
| `tests/contract/test_claude_code_stream.py` | Pins Claude Code CLI version, asserts event schema | New — runs CLI in CI |
| `docs/security.md` | v1 threat model, explicit scope | New — **write BEFORE launch** |

---

## Verification plan

**End-to-end smoke tests (Day 10 developer track, Day 13 operator track):**

1. **Pedro PR reviewer daemon (Day 10):**
   - `mars deploy examples/pr-reviewer-agent.yaml` → URL returned
   - Open chat URL → "review the latest commit on epic/agents-v2"
   - Daemon invokes bash, git, reads code, posts review as chat message with tool cards
   - Close laptop → 1 hour later reopen → daemon still alive, chat resumable
   - `mars ssh pr-reviewer` → shell access works

2. **Multi-session on one VM (Day 10):**
   - Deploy 3 agents from 3 agent.yaml files to same workspace
   - Dashboard shows all 3 with name + description
   - Chat with each, verify isolation (session A state doesn't bleed into B)
   - Kill machine → restart → `supervisor_recovery.py` marks sessions "needs restart" → user clicks resume → works

3. **Local mode (Day 10):**
   - `mars run --local examples/pr-reviewer-agent.yaml`
   - Spawns local `claude` subprocess, terminal interaction, same agent.yaml

4. **CLAUDE.md immutability (Day 10):**
   - Deploy daemon with specific CLAUDE.md
   - In chat, tell daemon "please update your CLAUDE.md to add instruction X"
   - Verify: PreToolUse hook blocks Edit attempt, daemon reports blocked
   - Web UI prompt editor → edit succeeds → session restarts with new prompt

5. **Secrets speed bump (Day 10):**
   - Deploy with secret `ZOHO_API_KEY`
   - Daemon can use it via a configured tool (MCP)
   - Daemon attempts `echo $ZOHO_API_KEY` → PreToolUse hook blocks
   - `docs/security.md` explicitly documents the residual risk

6. **Memory capture (Day 10):**
   - 30-min session with multiple turns
   - `mars memory export pr-reviewer` → produces bundle with session history, tool log, CLAUDE.md diff proposals
   - S3 object exists with correct key

7. **Magic-link signup (Day 7):**
   - Fresh email → enter on Mars homepage → receive magic link via Resend
   - Click link → JWT session cookie set → dashboard loads
   - Refresh browser → still authenticated
   - Logout → protected routes redirect to signup

8. **Maat operator track (Day 13):**
   - Fresh Mars account (new email via magic-link signup)
   - Dashboard → Templates → "Tracker Ops Assistant" → Start
   - Onboarding: Anthropic OAuth (redirected to Claude.ai), returns with token
   - Onboarding asks for Zoho API key, WhatsApp MCP config
   - "Deploy" button → spinner → chat opens
   - From phone: "resume los últimos 10 mensajes de WhatsApp"
   - Daemon uses WhatsApp MCP → returns summary → Maat replies in chat
   - **Verify: Maat did not touch a terminal, did not see a YAML file, did not type a CLI command at any point**

---

## Final verdict

**GO.** All four v1 decisions locked. The two-plane split is right, Fly machine model is right, Camtom pattern reuse is right (with the HTTP-forwarder topology flip and SSE fanout on control plane).

Execution risks concentrated in:
1. **Claude Code headless OAuth** — parallel spike Day 1 AM, bounded failure pivot to API-key mode.
2. **stream-json parser** (`session/claude_code_stream.py`) — 3-day budget Day 3 with contract test in CI, schema fixture from Day 1 spike.
3. **Permission prompt round-trip** — parallel spike Day 2 AM, bounded failure pivot to `acceptEdits` mode.
4. **Maat cold boot timing** — measured Day 5, addressed via warm-pool strategy if >30s.

With Pedro's decision to spike in parallel rather than doing a Day 0, the 10-day build timeline stays nominal IF spikes 1-3 pass. If they fail, the architecture has documented fallbacks that preserve the timeline at a reduced feature level.

**Buffer tightness acknowledged:** 13 days total (10 build + 1 Maat template + 1 security hardening + 1 Maat onboarding call). Magic-link auth is squeezed into Day 7 alongside chat UI work. If Maat template or auth take longer than budgeted, ship slips 1-3 days — still within a 2-week window.

The single sentence version: *Spike Claude Code CLI on Day 1-2 while building foundation, flip SSE direction to match Camtom, ship both the developer CLI and the Maat-shaped turnkey template in the same 13 days, and you land v1 with a real customer on it.*
