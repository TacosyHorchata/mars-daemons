# Mars

> A planet for your daemons.

Mars is a cloud platform for hosting AI agents ("daemons") powered by Claude Code and OpenAI Codex. You bring your own LLM account; Mars handles hosting, orchestration, persistence, and the web chat interface so your agents survive when you close your laptop.

## What Mars is (and isn't)

**Mars is:**
- A cloud runtime where Claude Code / Codex sessions run long-lived, not ephemerally
- A web chat UI so you can talk to your running agents from anywhere (phone, browser, new machine)
- A multi-session orchestrator — run many specialized daemons in parallel, each defined by a single `agent.yaml`
- A data moat foundation — every session's history, tool calls, and correction proposals are captured for your ops improvement
- BYOLLM: you connect your own Anthropic / OpenAI account; Mars does not mark up tokens

**Mars isn't:**
- A new agent runtime — it hosts Claude Code and Codex, it doesn't compete with them
- A general-purpose compute platform — it's opinionated about the agent workflow
- Public yet — v1 is in stealth, design-partner-only

## Status

**v1 code-complete, pre-deploy.** 40/47 stories landed, ~513 tests
passing across runtime, control plane, and CLI. The last 7 stories
are all gated on a live Fly machine (dev dogfood + OAuth flow half
+ Maat setup call + mobile real-phone E2E) and are scheduled as the
first items in [v1.1](docs/v1.1-backlog.md).

Design Partner #1: Maat (CEO of Orion, Mérida/Celaya).

See [`docs/planning/epics/index.md`](docs/planning/epics/index.md) for
the epic breakdown and [`docs/v1.1-backlog.md`](docs/v1.1-backlog.md)
for everything explicitly deferred out of v1 scope.

### What works today

- **Runtime supervisor** (`apps/mars-runtime/`) — FastAPI app that
  spawns `claude` / `codex` subprocesses, parses stream-json output
  into typed `MarsEvent`s, manages session lifecycle with a hard
  cap + per-session cwd, survives supervisor restart via
  `PersistedSessionHandle` atomic writes, captures memory to S3
- **Control plane** (`apps/mars-control/backend/`) — FastAPI app
  with magic-link auth (Resend + JWT cookie, rate-limited), SSE
  fanout for browser chat, event ingest with X-Event-Secret, session
  proxy to the runtime, template discovery (`GET /templates`), admin
  prompt edit flow (`PATCH /agents/{name}/prompt`)
- **Frontend** (`apps/mars-control/frontend/`) — Next.js 16 + React
  19 + Tailwind 4, auth-guarded dashboard with sessions + templates
  tabs, chat view with 4 bubble components (assistant text, tool
  call, tool result, permission request) backed by native
  EventSource, onboarding wizard steps 1-3 in Spanish
- **CLI** (`packages/mars-cli/`) — `mars init`, `mars deploy`,
  `mars ssh`, `mars run --local`, `mars edit-prompt`, `mars memory`
- **Security hooks** — PreToolUse deny on CLAUDE.md/AGENTS.md edit
  and `env`/`printenv`/`echo $`/`set` at command position
- **Local dev harness** — `mars_control.local_server` +
  `uvicorn --factory supervisor:create_app` run the full stack on
  `localhost:{3000,8000,8090}` with hardcoded dev secrets and an
  `InMemoryEmailSender` outbox inspectable at `/dev/outbox`

### What's deferred to v1.1

See [`docs/v1.1-backlog.md`](docs/v1.1-backlog.md). The short list:
Anthropic OAuth flow, live Fly deploy + cold-boot timing, dev-track
dogfood, Maat setup call, mobile real-phone E2E, persisted session
registry, SSE reconnect with Last-Event-ID, permission round-trip UI.

## Repo structure (v1 target)

```
mars-daemons/
├── apps/
│   ├── mars-control/          # Next.js + FastAPI — auth, orchestration, UI
│   │   ├── backend/
│   │   └── frontend/
│   └── mars-runtime/          # FastAPI supervisor running inside each Fly machine
├── packages/
│   └── mars-cli/              # `mars` CLI — deploy, ssh, run --local, edit-prompt
├── tests/
│   └── contract/              # Pinned Claude Code CLI contract tests
├── docs/
│   ├── planning/              # v1 plan + epics (the HOW we're building)
│   │   ├── v1-plan.md
│   │   └── epics/
│   ├── agent-yaml-spec.md     # (v1)
│   ├── getting-started.md     # (v1)
│   ├── oauth-setup.md         # (v1)
│   └── security.md            # (Epic 9) v1 threat model
└── examples/
    ├── pr-reviewer-agent.yaml # Pedro's dogfood daemon
    └── orion-daemon.yaml      # Reference for operator track
```

## Glossary

- **Mars** — the platform (the product, the cloud, the company)
- **daemon** — an individual agent instance deployed on Mars. Defined by a single `agent.yaml` (or `CLAUDE.md` + metadata).
- **agent.yaml** — the declarative unit of truth for a daemon. Lists runtime, system prompt, MCPs, tools, env, and secrets.
- **session** — a running execution of a daemon inside a Fly machine. One daemon can have multiple sessions over time.

## Canonical usage (v1 target)

```bash
# Scaffold a new daemon in the current directory
mars init

# Deploy to the cloud
mars deploy ./agent.yaml
# → https://mars.dev/chat/<daemon-id>

# Open a shell inside the machine (for devs)
mars ssh my-daemon

# Run locally, terminal I/O (for devs)
mars run --local ./agent.yaml

# Edit the daemon's instructions (admin only)
mars edit-prompt my-daemon

# Export session memory for inspection
mars memory export my-daemon
```

---

Built by [@TacosyHorchata](https://github.com/TacosyHorchata). v1 is private until we've shipped a real customer on it.
