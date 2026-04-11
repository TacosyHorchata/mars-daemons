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

**v1 in progress.** Shipping in ~13 days per the [v1 plan](docs/planning/v1-plan.md).
Design Partner #1: Maat (CEO of Orion, Mérida/Celaya).

See [`docs/planning/epics/index.md`](docs/planning/epics/index.md) for the epic breakdown.

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
