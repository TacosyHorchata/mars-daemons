# mars-runtime

> Minimal agent runtime. One script, pluggable LLM backend, stdin/stdout.

Inspired by [The Emperor Has No Clothes](https://www.mihaileric.com/The-Emperor-Has-No-Clothes/) — a coding agent is fundamentally a nested loop around an LLM with a few tools. `mars-runtime` is ~880 LoC of Python that implements exactly that, with no framework, no supervisor, no message bus.

## What it is

- **One script** — `python -m mars_runtime ./agent.yaml`. Reads user turns from stdin, emits events as JSON lines to stdout.
- **Provider-neutral `llm_client`** — a `Protocol` with one concrete `AnthropicClient`. Drop in OpenAI/Azure/etc. without touching the loop.
- **7 tools** — `read`, `list`, `edit`, `bash`, `grep`, `glob`, `websearch`. Self-register. Allowlist per agent via `agent.yaml`.
- **No FastAPI, no control plane, no S3 memory sync** — memory is the filesystem, events are stdout, lifetime is the process.

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
workdir: /tmp/hello
tools: [read, list, bash]
EOF

echo "You are a friendly assistant. Be concise." > CLAUDE.md

ANTHROPIC_API_KEY=sk-... PYTHONPATH=src \
  uv run python -m mars_runtime ./agent.yaml <<< "what files are in /tmp?"
```

Output is one JSON event per line:

```json
{"type": "session_started", "ts": "...", "name": "hello", "model": "claude-opus-4-5", ...}
{"type": "user_input", "ts": "...", "text": "what files are in /tmp?"}
{"type": "tool_call", "ts": "...", "id": "tu_01", "name": "list", "input": {"path": "/tmp"}}
{"type": "tool_result", "ts": "...", "id": "tu_01", ...}
{"type": "assistant_text", "ts": "...", "text": "..."}
{"type": "turn_completed", "ts": "...", "stop_reason": "end_turn"}
{"type": "session_ended", "ts": "...", ...}
```

## Layout

```
mars-runtime/
├── src/mars_runtime/
│   ├── __main__.py       # entry point
│   ├── agent.py          # the loop (outer: stdin, inner: tool_use)
│   ├── llm_client.py     # Protocol + AnthropicClient
│   ├── schema.py         # AgentConfig (pydantic)
│   ├── events.py         # stdout JSON-line emitter
│   └── tools/
│       ├── __init__.py   # Tool, ToolOutput, ToolRegistry
│       ├── read.py / listdir.py / edit.py
│       ├── bash.py / grep.py / glob.py
│       └── websearch.py
├── tests/                # pytest
├── examples/             # sample agent.yaml files
├── Dockerfile            # python:3.11-slim + ripgrep
└── pyproject.toml
```

## agent.yaml schema

```yaml
name: my-daemon              # fly.io-app-safe slug
description: what it does
model: claude-opus-4-5       # any Anthropic model id
system_prompt_path: ./CLAUDE.md
workdir: /workspace/my-daemon   # absolute path; process chdirs here
max_tokens: 8192             # per LLM call; provider enforces upper bound
tools: [read, list, bash]    # empty = all registered tools
env: [GITHUB_TOKEN]          # names forwarded by the deploy layer
```

## Security model

**Speed bump, not sandbox.** The `bash` tool blocks obvious secret reads (`env`, `printenv`, `echo $VAR`). The `edit` tool blocks `CLAUDE.md`/`AGENTS.md`/`agent.yaml` by basename. Both are trivially bypassable by a determined agent. Real isolation is a Docker/Fly concern — read-only FS, seccomp, scrubbed env.

`mars-runtime` assumes the daemon runs code you wrote, using keys you own.

## Events

| Type | Payload | When |
|---|---|---|
| `session_started` | name, model, cwd, tools | process start |
| `user_input` | text | each stdin turn |
| `assistant_text` | text | when model produces text |
| `tool_call` | id, name, input | model requests a tool |
| `tool_result` | id, name, content, is_error | after tool execution |
| `turn_completed` | stop_reason | inner loop exits with no tool_calls |
| `turn_truncated` | stop_reason, iteration | `stop_reason == "max_tokens"` |
| `turn_aborted` | reason | iteration cap or malformed tool_use_ids |
| `session_ended` | stop_reason | process exit |

## Docker

```bash
docker build -t mars-runtime .
docker run -e ANTHROPIC_API_KEY=sk-... -v $PWD/agent:/workspace mars-runtime
```

## Status

- 114 tests passing
- 4 rounds of adversarial review via independent model
- v0.1 deferred: path confinement, real bash sandbox, bounded message history, OpenAI backend, streaming `assistant_chunk`
