# Epic 1 — Supervisor & stream-json Parser

**Status:** `[ ]` not started
**Days:** 2 PM (supervisor skeleton starts) → 3 (parser — full day)
**Depends on:** Epic 0 (needs `AgentConfig` schema + `stream_json_sample.jsonl` fixture)
**Downstream blockers:** Epic 2, 3, 5, 6 (nothing else works until the supervisor can spawn sessions and emit events)
**Risk level:** **CRITICAL** — contains the highest-risk file in the project

## Summary

Build `mars-runtime` — the FastAPI supervisor that runs inside each user's Fly machine and spawns/manages Claude Code subprocesses per session. The core of this epic is `session/claude_code_stream.py`, the JSONL parser that translates Claude Code's `stream-json` output into Mars's event schema. This parser is the highest-risk file in v1 and has a 3-day budget with a contract test pinned to a specific Claude Code CLI version.

## Context

The plan agent's critical insight: there is NO stable Python SDK for Claude Code. The supervisor is a **translator**, not an agent runtime. Its job is to spawn `claude -p --output-format stream-json` as a subprocess, parse the JSONL events, map them to Mars's event schema (durable + ephemeral split, mirroring Camtom's pattern at `services/fastapi/src/products/agents/agent/events.py:18-111`), and expose a control API so the control plane can spawn/list/kill sessions and inject user messages.

This epic is where v1 stops being hypothetical and starts being real engineering.

## Scope

### In scope
- `apps/mars-runtime/src/supervisor.py` — FastAPI app exposing the control API
- `apps/mars-runtime/src/session/manager.py` — `SessionManager` that owns the in-memory `active_sessions: dict[str, SessionHandle]` map
- `apps/mars-runtime/src/session/claude_code.py` — Subprocess lifecycle (spawn, monitor, kill) wrapping `asyncio.create_subprocess_exec(['claude', '-p', ...])`
- `apps/mars-runtime/src/session/claude_code_stream.py` ★ — The JSONL parser. Maps `stream-json` events (`system_init`, `assistant`, `user` tool_result, `result`) → Mars event schema
- `apps/mars-runtime/src/session/permissions.py` — Permission prompt interception + response round-trip (falls back to `acceptEdits` mode if Spike 3 failed)
- `apps/mars-runtime/src/events/types.py` — `MarsEvent` type hierarchy with `DURABLE_EVENTS` + `EPHEMERAL_EVENTS` split (mirrors Camtom pattern)
- `tests/contract/test_claude_code_stream.py` — Runs the actual `claude` CLI against the fixture from Epic 0, asserts event schema, **fails CI if Claude Code version bumps break the contract**

### Control API (inside supervisor.py)
- `POST /sessions` — body: `AgentConfig`, returns `session_id`
- `GET /sessions` — returns list of active sessions with name + description + status
- `GET /sessions/{id}` — returns full session handle
- `DELETE /sessions/{id}` — kills the subprocess
- `POST /sessions/{id}/input` — body: `{text: str}`, injects user message into running session
- `POST /sessions/{id}/permission-response` — body: `{approved: bool, permission_id: str}`
- `GET /sessions/{id}/events` — returns the event history (for replay on reconnect)

### Out of scope (deferred)
- Event forwarding to control plane (Epic 2 — for now, events just go into an in-memory queue)
- Persistent session handles on volume (Epic 5 — for now, sessions die on supervisor restart)
- Codex subprocess support (Epic 3 — Claude Code only in this epic)
- CLAUDE.md immutability enforcement (Epic 6 — for now, files are writeable)
- Memory capture (Epic 6 — for now, session history stays in subprocess working dir)

## Acceptance criteria

- [ ] `apps/mars-runtime/src/events/types.py` defines `MarsEvent` base + subtypes: `AssistantText`, `ToolCall`, `ToolResult`, `PermissionRequest`, `TurnCompleted`, `SessionStarted`, `SessionEnded`. Durable vs ephemeral split matches Camtom pattern.
- [ ] `claude_code_stream.py` exposes `async def parse_stream(stdout: asyncio.StreamReader) -> AsyncIterator[MarsEvent]` that yields typed events as they arrive
- [ ] `tests/contract/test_claude_code_stream.py` runs the real `claude` CLI (pinned version) and verifies every event in `stream_json_sample.jsonl` maps to a `MarsEvent` without loss
- [ ] Running the supervisor locally + `curl -X POST /sessions -d @pr-reviewer-agent.yaml` → returns a session_id + spawns a working `claude` subprocess
- [ ] `curl GET /sessions` lists the active session with correct name + description
- [ ] `curl POST /sessions/{id}/input -d '{"text": "what is 2+2?"}'` → subprocess receives the message + produces events
- [ ] Events arrive in an in-memory queue (polling-based for this epic; Epic 2 replaces with HTTP forwarding)
- [ ] `curl DELETE /sessions/{id}` cleanly kills the subprocess (no zombies)
- [ ] Supervisor handles 3 concurrent sessions without crashing
- [ ] Contract test runs in CI and fails if Claude Code version drifts

## Critical files

| File | Purpose |
|---|---|
| `apps/mars-runtime/src/supervisor.py` | FastAPI control API |
| `apps/mars-runtime/src/session/manager.py` | `SessionManager` + `active_sessions` dict |
| `apps/mars-runtime/src/session/claude_code.py` | Subprocess lifecycle |
| `apps/mars-runtime/src/session/claude_code_stream.py` ★ | **JSONL parser — highest-risk file** |
| `apps/mars-runtime/src/session/permissions.py` | Permission prompt round-trip |
| `apps/mars-runtime/src/events/types.py` | `MarsEvent` hierarchy, durable/ephemeral split |
| `tests/contract/test_claude_code_stream.py` | Contract test with pinned CC version |

## Dependencies

- **Upstream:** Epic 0 (`AgentConfig`, `stream_json_sample.jsonl`)
- **Downstream:**
  - Epic 2 needs the events emitted here
  - Epic 3 containerizes this
  - Epic 5 adds recovery + multi-session
  - Epic 6 adds immutability + memory
  - Epic 7 dogfoods end-to-end

## Risks

| Risk | Mitigation |
|---|---|
| stream-json schema is more complex than the spike showed | Budget 3 full days. Contract test catches drift. If you hit a 4th day, redesign the parser as a state machine instead of a dispatch table. |
| Permission prompts don't round-trip cleanly | Fallback: `--permission-mode acceptEdits` + PreToolUse denylist hook. v1 has reduced human-in-loop, documented limitation. |
| Subprocess cleanup leaks (zombie `claude` processes) | `asyncio.create_subprocess_exec` with explicit kill on shutdown; integration test that spawns/kills 10 sessions and asserts no zombies via `ps`. |
| In-memory `active_sessions` lost on supervisor restart | Accepted limitation in this epic. Epic 5 adds volume-persistent handles + reconciliation. |
| Event queue backpressure (slow consumer → unbounded memory) | Use `asyncio.Queue(maxsize=1000)` per session; drop ephemeral events on overflow, never drop durable events. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] Contract test passing in CI with pinned Claude Code CLI version
- [ ] Integration test: 3 concurrent sessions, spawn + input + kill cycle, no zombies
- [ ] Supervisor can run locally via `uv run mars-runtime` and responds to curl on all endpoints
- [ ] Docstrings on every public class/function in `session/` and `events/types.py`
- [ ] Next epic (Event Forwarding) can import `MarsEvent` and `SessionManager` cleanly

## Stories

Total: **6 stories**, ~14h budget. Contains the highest-risk file in the project (`claude_code_stream.py`) — Stories 1.2 and 1.3 are the most load-bearing in all of v1.

- [x] **Story 1.1 — `MarsEvent` type hierarchy** (~2h)
  - *Goal:* Define `MarsEvent` base + subtypes (`AssistantText`, `ToolCall`, `ToolResult`, `PermissionRequest`, `TurnCompleted`, `SessionStarted`, `SessionEnded`) with durable/ephemeral split mirroring Camtom's `agent/events.py:18-111`.
  - *Files:* `apps/mars-runtime/src/events/types.py`, `tests/runtime/test_event_types.py`, `pyproject.toml`
  - *Done when:* unit test classifies each event type correctly and a fixture event round-trips
  - *Outcome:* 9 concrete subtypes wired as a Pydantic v2 discriminated union via `MARS_EVENT_ADAPTER` (`TypeAdapter`). Durable/ephemeral split matches Camtom. Codex adversarial review surfaced boundary-contract issues which were all fixed before commit: strict-mode numeric fields with `ge=0` (reject bool/str coercion and negatives), `timestamp` as UTC-aware `datetime` (JSON-serialized to ISO), `Any` → `JsonValue` on tool inputs and permission_denials, `message_id` + `block_index` correlation fields on every per-block event (assistant_text/chunk, tool_call/result) for deterministic multi-block turn reconstruction, and a `model_validator` enforcing that ephemeral events must not carry a sequence. 21 unit tests in `tests/runtime/test_event_types.py`; full suite 52/52.

- [ ] **Story 1.2 — ★ `claude_code_stream.py` parser v1 (happy path)** (~3h)
  - *Goal:* Async JSONL parser mapping stream-json `system_init`, `assistant`, `user` tool_result, and `result` events to `MarsEvent` subtypes via dispatch table.
  - *Files:* `apps/mars-runtime/src/session/claude_code_stream.py`
  - *Done when:* parser consumes the `stream_json_sample.jsonl` fixture and yields typed events without loss

- [ ] **Story 1.3 — ★ Parser v2 + contract test** (~3h)
  - *Goal:* Edge cases (partial messages, tool_result errors, unknown event types) + contract test that runs pinned `claude` CLI in CI and fails on schema drift.
  - *Files:* `apps/mars-runtime/src/session/claude_code_stream.py`, `tests/contract/test_claude_code_stream.py`
  - *Done when:* contract test passes against pinned Claude Code CLI version in CI

- [ ] **Story 1.4 — `SessionManager` + subprocess lifecycle** (~3h)
  - *Goal:* `SessionManager` owning the in-memory `active_sessions` dict + `claude_code.py` spawning/monitoring/killing subprocesses via `asyncio.create_subprocess_exec`.
  - *Files:* `apps/mars-runtime/src/session/manager.py`, `apps/mars-runtime/src/session/claude_code.py`
  - *Done when:* spawning + killing 10 sessions leaves zero zombie processes (asserted via `ps`)

- [ ] **Story 1.5 — `supervisor.py` FastAPI control API** (~2h)
  - *Goal:* FastAPI app exposing `POST/GET/DELETE /sessions`, `/sessions/{id}/input`, `/sessions/{id}/events` endpoints with in-memory event queue.
  - *Files:* `apps/mars-runtime/src/supervisor.py`
  - *Done when:* `curl POST /sessions -d @pr-reviewer-agent.yaml` returns a session_id and spawns a working subprocess

- [ ] **Story 1.6 — `permissions.py` round-trip (or fallback)** (~1h)
  - *Goal:* Permission prompt interception + `/sessions/{id}/permission-response` round-trip, with `acceptEdits` + denylist fallback if Spike 3 failed.
  - *Files:* `apps/mars-runtime/src/session/permissions.py`
  - *Done when:* a tool-approval prompt in a running session can be approved or denied via the supervisor API

## Notes

- **The parser is a dispatch table, not a state machine, for v1.** Each incoming stream-json event has a `type` field; map it to a function. If the state machine temptation hits, resist — it's premature abstraction at this stage.
- **Pin the Claude Code CLI version** in a file like `apps/mars-runtime/.tool-versions` or similar. Upgrading is a deliberate act, not a consequence of `brew update`.
- The in-memory queue is fine for Epic 1 because the supervisor and the "consumer" (control plane proxy) will run in the same process until Epic 2 splits them.
- Do NOT add Codex support here. Epic 3 does that. Adding it now doubles the surface area and risk of this already-risky epic.
- **Camtom reference:** read `services/fastapi/src/products/agents/agent/events.py` lines 18-111 to see the durable/ephemeral pattern before writing `events/types.py`. Copy the shape, not the imports.
