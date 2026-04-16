# mars-runtime

> Minimal agent runtime with persistent sandbox + resumable sessions. One script, pluggable LLM backend, stdin/stdout.

Inspired by [The Emperor Has No Clothes](https://www.mihaileric.com/The-Emperor-Has-No-Clothes/) — a coding agent is fundamentally a nested loop around an LLM with a few tools. `mars-runtime` is ~1000 LoC of Python that implements exactly that, plus JSON-per-session persistence and per-turn git commits of the workspace.

## What it is

- **One script** — `python -m mars_runtime <agent.yaml>` starts a session. `--resume <id>` continues one. `--list` shows recent sessions.
- **Provider-neutral `llm_client`** — a `Protocol` with one concrete `AnthropicClient`. Drop in OpenAI/Azure/etc. without touching the loop.
- **7 tools** — `read`, `list`, `edit`, `bash`, `grep`, `glob`, `websearch`. Self-register. Allowlist per agent.
- **Persistent sandbox** — Docker volume + git repo per entorno. World state is git. Dialogue state is one JSON file per session. Supervisor writes both — agent can't forge or skip them.
- **CLI-first runtime with an optional local HTTP daemon** — memory is the filesystem.

## The model

```
Entorno  = one Fly volume (or Docker volume).  Identity, sandbox, memory.
Agent    = one agent.yaml.                      Stateless recipe / role.
Session  = one invocation.                      Persisted as JSON, resumable.
```

Container filesystem:

```
/data/workspace/   R/W   git repo; agent cwd; tools operate here.
/data/sessions/    R/W   <id>.json snapshots (supervisor-only convention).
/tmp/              tmpfs per-turn scratch.
```

## Quick start

```bash
git clone https://github.com/TacosyHorchata/mars-daemons.git
cd mars-daemons
uv sync

cat > agent.yaml <<EOF
name: hello
description: a minimal daemon
model: claude-opus-4-5
system_prompt_path: ./CLAUDE.md
tools: [read, list, bash]
EOF

echo "You are a friendly assistant. Be concise." > CLAUDE.md

export ANTHROPIC_API_KEY=sk-...
export MARS_DATA_DIR=./.mars-data

# New session
uv run python -m mars_runtime ./agent.yaml <<< "what files are in /tmp?"

# List recent sessions
uv run python -m mars_runtime --list

# Resume the most recent session
SESS=$(ls -t .mars-data/sessions/sess_*.json | head -1 | xargs basename -s .json)
uv run python -m mars_runtime --resume "$SESS" <<< "what did we find?"
```

Output is one JSON event per line:

```json
{"type": "session_started", "session_id": "sess_abc123...", "name": "hello", "model": "claude-opus-4-5"}
{"type": "user_input", "text": "what files are in /tmp?"}
{"type": "tool_call", "id": "tu_01", "name": "list", "input": {"path": "/tmp"}}
{"type": "tool_result", "id": "tu_01", ...}
{"type": "assistant_text", "text": "..."}
{"type": "turn_completed", "stop_reason": "end_turn"}
{"type": "session_saved", "session_id": "sess_abc123..."}
{"type": "turn_committed", "commit_sha": "abc...", "turn_number": 1}
{"type": "session_ended", "stop_reason": "end_turn"}
```

## CLI

```
python -m mars_runtime <agent.yaml>     # start a new session
python -m mars_runtime --resume <id>    # resume a session (no yaml needed)
python -m mars_runtime --list           # list recent sessions as JSON lines
python -m mars_runtime --data-dir DIR ... # override $MARS_DATA_DIR
```

## HTTP daemon

The daemon is a localhost HTTP adapter over the same runtime. It is single-tenant by design: one fixed `agent.yaml`, bearer-file auth, session snapshots under `MARS_DATA_DIR/sessions`, and a SQLite `turns.db` for active-turn bookkeeping.

```bash
export ANTHROPIC_API_KEY=sk-...
export MARS_DATA_DIR=./.mars-data
export MARS_AUTH_TOKEN_FILE=/tmp/mars-daemon-token
printf 'secret-token\n' > "$MARS_AUTH_TOKEN_FILE"
chmod 600 "$MARS_AUTH_TOKEN_FILE"

uv run python -m mars_runtime.daemon ./agent.yaml

curl -s \
  -H "Authorization: Bearer $(cat "$MARS_AUTH_TOKEN_FILE")" \
  -X POST http://127.0.0.1:8080/v1/sessions

curl -N \
  -H "Authorization: Bearer $(cat "$MARS_AUTH_TOKEN_FILE")" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8080/v1/sessions/$SID/messages \
  -d '{"turn_id":"8d3a7c14-2601-4ea6-90db-bfe93f10bb5c","text":"hola"}'
```

HTTP auth is a network gate only; it does not change the tool trust model already documented below. An authenticated caller gets the same tool/RCE surface as the configured CLI agent.

If the SSE stream drops before a terminal event, that turn's streamed output is lost; the next turn works normally.

If the daemon crashes mid-turn, the in-flight row is recovered as `failed` in SQLite with `error='daemon_restart'`; the client receives no terminal event and must rely on its own timeout/retry behavior.

## agent.yaml schema

```yaml
name: my-daemon              # fly.io-app-safe slug
description: what it does
model: claude-opus-4-5       # any Anthropic model id
system_prompt_path: ./CLAUDE.md
max_tokens: 8192
tools: [read, list, bash]    # empty = all registered tools
env: [GITHUB_TOKEN]          # names forwarded by the deploy layer
workdir: /workspace          # legacy field; the CLI chdirs to $MARS_DATA_DIR/workspace
```

## Session file format

Each closed turn atomically rewrites `/data/sessions/<id>.json`:

```json
{
  "id": "sess_abc123def456",
  "agent_name": "hello",
  "agent_config": { "... full AgentConfig ..." },
  "created_at": 1712345678,
  "messages": [ "... Anthropic messages array ..." ]
}
```

Self-contained. `--resume` doesn't depend on the original yaml — if you delete or move it, the session still continues.

## Security model

**Speed bump, not sandbox.** The `bash` tool blocks obvious secret reads (`env`, `printenv`, `echo $VAR`). The `edit` tool blocks `CLAUDE.md`/`AGENTS.md`/`agent.yaml` by basename. Session files and git state are supervisor-only *by convention* — a determined agent with tools can still touch them. Real isolation is a Docker/Fly concern: read-only FS for `/app`, seccomp, scrubbed env.

`mars-runtime` assumes the daemon runs code you wrote, using keys you own.

## Events

| Type | Payload | When |
|---|---|---|
| `session_started` | name, model, cwd, tools, session_id | process start |
| `user_input` | text | each stdin turn |
| `assistant_text` | text | when model produces text |
| `tool_call` | id, name, input | model requests a tool |
| `tool_result` | id, name, content, is_error | after tool execution |
| `turn_completed` | stop_reason | inner loop exits with no tool_calls |
| `turn_truncated` | stop_reason, iteration | `stop_reason == "max_tokens"` |
| `turn_aborted` | reason | iteration cap or malformed tool_use_ids |
| `session_saved` | session_id | after end-of-turn snapshot write |
| `turn_committed` | commit_sha, turn_number | after git commit (only if workspace changed) |
| `session_ended` | stop_reason | process exit |

## Docker / Fly

```bash
docker build -t mars-runtime .

# Local run with a named volume for persistence
docker run --rm -i -e ANTHROPIC_API_KEY \
  -v mars_data:/data \
  -v $PWD/agent.yaml:/data/workspace/agent.yaml:ro \
  -v $PWD/CLAUDE.md:/data/workspace/CLAUDE.md:ro \
  mars-runtime /data/workspace/agent.yaml <<< "hi"
```

For Fly deployment see `fly.toml`. One app per entorno; one volume mounted at `/data`. To fork an entorno: `fly volume fork` (or `cp -r` for off-Fly).

## Status

- 95 tests passing
- 7 rounds of adversarial review via independent model

## Known limitations (v0.2.0)

- **No real path confinement on tools.** The `bash`, `read`, `edit`, `list`, `grep`, and `glob` tools can reach arbitrary absolute paths. The `/data/sessions/` directory being "supervisor-only" is a *naming convention*, not a security boundary. A determined agent can forge its own session state. Treat this as a documentation boundary, not enforced isolation.
- **No fsync on session writes.** `os.replace` is atomic but not durable — a hard crash after `session_saved` can still lose the latest turn. Acceptable for the single-writer model; revisit if deployed in volatile environments.
- **Commit-then-save is not transactional.** If git commit succeeds and the session JSON write fails, git has turn N but the session reflects turn N-1. Resume re-does conversational work; workspace commits are not re-applied. No corruption, but inconsistency is visible.
- **Requires git ≥ 2.28** on the host (for `git init -b main`). The provided Dockerfile is fine; plain host runs may need a newer git.
- **No bounded message history.** messages[] grows per turn until Anthropic's context window rejects the call. Whole-session JSON is rewritten each turn, so total I/O is O(N²) in turn count.
- **No concurrency control.** One session-writer per entorno is the contract; two simultaneous writes can race.

## Deferred for later versions

- Tool path confinement (real isolation)
- Real bash sandbox (seccomp / gVisor)
- Bounded message history (rolling summarization or turn-based pruning)
- OpenAI backend implementation of the LLMClient protocol
- Streaming `assistant_chunk` events
- Resumable mid-turn (currently crash mid-turn loses that turn)
- Multi-writer concurrency (locks or CRDT)
- Branching UX (fork an entorno at a specific turn via CLI, not just volume copy)
