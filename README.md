# Mars Daemons

> A planet for your daemons.

Mars Daemons is an open-source runtime for hosting long-lived AI agents ("daemons") powered by [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [OpenAI Codex](https://openai.com/index/codex/). You bring your own LLM account; Mars handles process supervision, stream-json parsing, event forwarding, session management, crash recovery, and security sandboxing.

## What it does

- **Spawns and supervises** `claude -p` / `codex` subprocesses inside a sandbox
- **Parses stream-json** output into typed `MarsEvent` Pydantic models (durable vs. ephemeral split)
- **Forwards events** outbound via HTTP to any control plane with `X-Event-Secret` auth
- **Manages sessions** with a hard cap per machine, per-session cwd, and atomic persistence
- **Recovers from crashes** — `PersistedSessionHandle` restores session state on supervisor restart
- **Captures memory** per session with S3 sync
- **Sandboxes agents** via `claude_code_settings.json` PreToolUse hooks that deny prompt editing and secret exfiltration
- **Deploys to Fly.io** — one machine per daemon, zero-downtime via `mars deploy`

## Quick start

```bash
# Clone
git clone https://github.com/TacosyHorchata/mars-daemons.git
cd mars-daemons

# Set up Python env
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .

# Create your first agent
cat > my-agent.yaml <<EOF
name: hello-world
description: A minimal daemon that greets you.
runtime: claude-code
system_prompt_path: ./CLAUDE.md
EOF

echo "You are a friendly assistant." > CLAUDE.md

# Run locally (no deploy needed)
mars run --local ./my-agent.yaml
```

## Repo structure

```
mars-daemons/
├── apps/
│   └── mars-runtime/          # FastAPI supervisor — one per Fly machine
│       └── src/
│           ├── supervisor.py   # App factory, session pool, /health
│           ├── session/        # claude_code.py, codex.py, manager, stream parser
│           ├── events/         # MarsEvent types, HttpEventForwarder
│           └── schema/         # agent.yaml AgentConfig (Pydantic)
├── packages/
│   └── mars-cli/              # `mars` CLI — deploy, ssh, run --local, edit-prompt
│       └── src/mars/
│           ├── deploy.py       # mars deploy (Fly.io machines API)
│           ├── fly/client.py   # Async Fly.io REST wrapper
│           └── runtime_local.py
├── tests/
│   ├── runtime/               # Supervisor, session, event pipeline
│   ├── schema/                # agent.yaml validation
│   ├── cli/                   # CLI commands + Fly client
│   └── contract/              # Pinned Claude Code CLI contract tests
├── examples/
│   └── pr-reviewer-agent.yaml # Reference: reviews PRs on a target repo
├── docs/
│   └── planning/              # v1 plan + epic trackers
└── pyproject.toml
```

## agent.yaml schema

Every daemon is defined by a single `agent.yaml`:

```yaml
name: pr-reviewer                    # slug, lowercase, max 30 chars
description: Reviews open PRs.       # human-readable
runtime: claude-code                 # claude-code | codex (v1.1)
system_prompt_path: ./CLAUDE.md      # relative to workdir
workdir: /workspace/pr-reviewer      # absolute, default /workspace
mcps:                                # MCP servers to enable
  - github
tools:                               # Claude Code tools to allow
  - Bash
  - Read
  - Grep
env:                                 # env vars forwarded from parent
  - GITHUB_TOKEN
  - ANTHROPIC_API_KEY
```

## CLI commands

| Command | What it does |
|---|---|
| `mars init` | Scaffold a new `agent.yaml` in the current directory |
| `mars deploy ./agent.yaml` | Deploy to a Fly.io machine |
| `mars ssh <daemon>` | Shell into a running machine |
| `mars run --local ./agent.yaml` | Run locally, terminal I/O |
| `mars edit-prompt <daemon>` | Edit CLAUDE.md (admin-only, restarts supervisor) |
| `mars memory export <daemon>` | Export session memory |

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Fly Machine (one per daemon)                    │
│                                                  │
│  ┌──────────────┐     ┌───────────────────────┐ │
│  │  Supervisor   │────▶│  claude -p / codex    │ │
│  │  (FastAPI)    │◀────│  (subprocess)         │ │
│  │  port 8080    │     └───────────────────────┘ │
│  └──────┬───────┘                                │
│         │ HttpEventForwarder                     │
│         │ POST /internal/events                  │
│         │ X-Event-Secret header                  │
└─────────┼───────────────────────────────────────┘
          │
          ▼
    Control Plane (your own, or any HTTP endpoint)
```

The supervisor is agnostic of what control plane it talks to. It forwards events outbound and accepts session input inbound. The only contract is HTTP + the `agent.yaml` schema.

## Development

```bash
# Install dev deps
pip install -e ".[dev]"

# Run tests
pytest                          # 373 tests, ~3s

# Run the supervisor locally (port 8090)
PYTHONPATH="apps/mars-runtime/src:packages/mars-cli/src" \
  python -m uvicorn --factory --port 8090 supervisor:create_app
```

## License

Apache 2.0 — see [LICENSE](LICENSE).

---

Built by [@TacosyHorchata](https://github.com/TacosyHorchata).
