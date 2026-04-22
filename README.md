# mars-daemons

Standalone backend built on top of the exportable `agents_v2` core. The repo now treats `mars-daemons` as a host around a reusable kernel instead of a custom one-off runtime.

## What ships now

- `mars_runtime.core`: copied from `agents_v2` and kept domain-agnostic.
- `mars_runtime.tools`: generic tools ported from `agents_v2`.
- `mars_runtime.host`: standalone FastAPI app with:
  - bearer auth via `MARS_AUTH_TOKEN`
  - local filesystem conversation store
  - SSE event streaming
  - local attachment storage
  - per-conversation persistent workspace
  - file-backed rules / skills / cross-conversation memory providers
  - builtin runtime tools: `read_memory`, `edit_memory`, `use_skill`, `storage`, `workspace`, `run_bash`

## Run

```bash
uv sync

export MARS_AUTH_TOKEN=secret-token
export MARS_MODEL=azure_ai/Kimi-K2.5
export MARS_DATA_DIR=./.mars-data

uv run mars-daemons --host 127.0.0.1 --port 8080
```

## API

- `POST /api/v1/agents/conversations`
- `GET /api/v1/agents/conversations`
- `GET /api/v1/agents/conversations/{conversation_id}`
- `POST /api/v1/agents/conversations/{conversation_id}/messages`
- `POST /api/v1/agents/conversations/{conversation_id}/cancel`
- `GET /api/v1/agents/conversations/{conversation_id}/events`
- `GET /api/v1/agents/files/{file_key}`
- `GET /api/v1/agents/conversations/{conversation_id}/workspace/{path}`

All protected endpoints require:

```http
Authorization: Bearer <MARS_AUTH_TOKEN>
```

Optional scoping headers:

- `X-Mars-Org-Id`
- `X-Mars-User-Id`

## Local data

`MARS_DATA_DIR` stores:

- `conversations/`: one JSON document per conversation
- `files/`: uploaded attachments
- `workspaces/`: per-org/per-user/per-conversation persistent workspace
- `memory.json`: cross-conversation memory
- `rules.json`: optional prompt rules
- `skills.json`: optional activatable skills

## Notes

- This rewrite intentionally drops the legacy CLI/session sandbox runtime in favor of the `agents_v2` backend split.
- Dynamic org-specific tooling and Camtom host features are still out of scope in this pass.
