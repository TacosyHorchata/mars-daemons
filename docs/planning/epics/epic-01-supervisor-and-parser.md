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

- [x] **Story 1.2 — ★ `claude_code_stream.py` parser v1 (happy path)** (~3h)
  - *Goal:* Async JSONL parser mapping stream-json `system_init`, `assistant`, `user` tool_result, and `result` events to `MarsEvent` subtypes via dispatch table.
  - *Files:* `apps/mars-runtime/src/session/__init__.py`, `apps/mars-runtime/src/session/claude_code_stream.py`, `tests/runtime/test_claude_code_stream.py`
  - *Done when:* parser consumes the `stream_json_sample.jsonl` fixture and yields typed events without loss
  - *Outcome:* Flat dispatch table (NOT a state machine) mapping `(type, subtype)` → handler. Happy path on the captured fixture yields the canonical `SessionStarted → ToolCall → ToolResult → AssistantText → SessionEnded` (rate_limit_event dropped). Async `parse_stream` + sync `parse_line` both covered. After codex adversarial review, tightened silent-failure modes: `system.init` with missing canonical fields now raises `CriticalParseError` (a non-`ParseError` exception that propagates through `parse_stream` to the supervisor), `is_error` strict-matches literal `True` only (rejects `"false"`/`1`), non-dict `tool_use.input` logs a warning instead of silently coercing, image-only tool_results emit a visible placeholder sentinel instead of an empty string. 26 unit tests, full suite 78/78.

- [x] **Story 1.3 — ★ Parser v2 + contract test** (~3h)
  - *Goal:* Edge cases (partial messages, tool_result errors, unknown event types) + contract test that runs pinned `claude` CLI in CI and fails on schema drift.
  - *Files:* `apps/mars-runtime/src/session/claude_code_stream.py`, `apps/mars-runtime/src/session/claude_code_version.py`, `tests/contract/test_claude_code_stream.py`, `tests/runtime/test_claude_code_stream.py`
  - *Done when:* contract test passes against pinned Claude Code CLI version in CI
  - *Outcome:* (1) `stream_event` handler maps Anthropic SSE `content_block_delta.text_delta` → `AssistantChunk` for `--include-partial-messages` sessions; other inner types drop defensively. Empirically captured the partial-message schema against pinned 2.1.101 first. (2) `parse_stream` now takes an `on_warning: WarningCallback` kwarg (typed `Exception | None`, not `BaseException`) so soft drops (bad JSON, non-object payloads) surface to the supervisor instead of disappearing. `CriticalParseError` still propagates for `system.init` drift. (3) `claude_code_version.py` pins `PINNED_CLAUDE_CODE_VERSION = "2.1.101"` with a bump checklist. (4) `tests/contract/test_claude_code_stream.py` runs a real `claude -p` session when `MARS_CONTRACT_LIVE=1` is set, asserts canonical Mars event sequence + zero warnings + exact version token match + drained stderr + returncode 0. Codex round 2 adversarial review flagged contract-test false-positives (substring version match, swallowed warnings, undrained stderr, missing returncode check, too-wide `BaseException` callback) — all fixed before commit. Local run against 2.1.101: 11s, 1 passed. Full suite: 84 passed, 1 skipped (contract test by design).

- [x] **Story 1.4 — `SessionManager` + subprocess lifecycle** (~3h)
  - *Goal:* `SessionManager` owning the in-memory `active_sessions` dict + `claude_code.py` spawning/monitoring/killing subprocesses via `asyncio.create_subprocess_exec`.
  - *Files:* `apps/mars-runtime/src/session/manager.py`, `apps/mars-runtime/src/session/claude_code.py`, `tests/runtime/test_session_manager.py`
  - *Done when:* spawning + killing 10 sessions leaves zero zombie processes (asserted via `ps`)
  - *Outcome:* `SessionManager` with injectable `spawn_fn` (tests use `/bin/sleep` stub, production wires `spawn_claude_code`). `pop` under lock + `wait()` outside so concurrent kills cannot double-claim. `claude_code.py` builds the command + env: explicit-allowlist env forwarding with nesting-leak scrub run *after* `extra` merge, `stdin=DEVNULL` in v1.4 (no `--input-format stream-json` until Story 1.5 wires input injection) so a pending stdin read cannot hang the child. Codex adversarial review caught three real issues — all fixed before commit: (1) status taxonomy widened from `{running, terminated, failed}` to `{running, exited_clean, killed, exited_error, kill_timeout}` with a `_classify_returncode` helper keyed on `-signal.SIGKILL`; (2) scrub order flipped so `extra` cannot reintroduce `CLAUDECODE`; (3) `shutdown()` now parallelizes via `asyncio.gather(return_exceptions=True)` so one stuck session cannot burn N×timeout; (4) SIGKILL timeouts tombstoned on a new `orphaned` property for observability. 17 unit tests, verified zero-zombie on 10 concurrent spawn/kill cycles via dual probe (`os.kill(pid, 0)` + `ps -p`). Full suite 101 passed, 1 skipped.

- [x] **Story 1.5 — `supervisor.py` FastAPI control API** (~2h)
  - *Goal:* FastAPI app exposing `POST/GET/DELETE /sessions`, `/sessions/{id}/input`, `/sessions/{id}/events` endpoints with in-memory event queue.
  - *Files:* `apps/mars-runtime/src/supervisor.py`, `apps/mars-runtime/src/session/claude_code.py`, `tests/runtime/test_supervisor_api.py`, `pyproject.toml`
  - *Done when:* `curl POST /sessions -d @pr-reviewer-agent.yaml` returns a session_id and spawns a working subprocess
  - *Outcome:* `create_app()` factory builds a FastAPI app wired to a `SessionManager`. Endpoints: `GET /health`, `POST /sessions` (accepts JSON or YAML payloads), `GET /sessions`, `GET/DELETE /sessions/{id}`, `POST /sessions/{id}/input` (writes stream-json user event via stdin), `GET /sessions/{id}/events` (drains queue), `POST /sessions/{id}/permission-response` (501 — deferred to v1.1). Each session gets a `_SessionPump` background task that runs `parse_stream` and fills a per-session `asyncio.Queue`. `spawn_claude_code` gained a `stdin_stream_json` kwarg; supervisor spawns with `True`, other callers stay on the 1.4-safe `False` default. Codex adversarial review caught six issues — all fixed: (1) pump now schedules `mgr.kill` on EOF or fatal parser error so sessions leave `running` state, (2) queue uses blocking `await put()` instead of `put_nowait` so durable events can never be dropped, (3) `except BaseException` narrowed to `except asyncio.CancelledError`, (4) `/input` drain now has a 5s `wait_for` with a 503 on timeout, (5) lifespan awaits cancelled pump tasks before calling `mgr.shutdown`, (6) 64 KB body-size cap on `POST /sessions` to prevent memory-DoS via giant YAML. 20 unit tests (stub subprocess that emits canonical stream-json and echoes stdin input). Full suite 121 passed, 1 skipped.

- [x] **Story 1.6 — `permissions.py` round-trip (or fallback)** (~1h)
  - *Goal:* Permission prompt interception + `/sessions/{id}/permission-response` round-trip, with `acceptEdits` + denylist fallback if Spike 3 failed.
  - *Files:* `apps/mars-runtime/src/session/permissions.py`, `tests/runtime/test_permissions.py`
  - *Done when:* a tool-approval prompt in a running session can be approved or denied via the supervisor API
  - *Outcome:* Took the fallback path per spike 3's finding that the full bidirectional permission-prompt wire schema is not yet stable. `PermissionPolicy` is a frozen dataclass capturing v1's three-layer model: (a) `acceptEdits` mode always; (b) the daemon's `tools` list becomes the `--allowed-tools` allowlist at launch; (c) PreToolUse hook denylist baked into `claude_code_settings.json` (Epic 3 bakes the file). `derive_policy(AgentConfig)` builds the value object; `build_claude_code_settings(policy)` renders the exact dict shape for the settings file with regression-guarded hooks for CLAUDE.md / AGENTS.md / claude_code_settings.json immutability and secret-read Bash patterns (`env`, `printenv`, `echo $`, `set`). The supervisor's `/permission-response` endpoint stays at 501 — the fallback decision is honest: v1 cannot approve/deny dynamically, the static allowlist + denylist *is* the approve/deny surface, and users inject input via `/input`. Full round-trip with a pending-prompt event is tracked in `spikes/03-permission-roundtrip.md` for v1.1. 12 unit tests cover policy derivation, settings.json shape, JSON-serializability, and regression guards on the immutability/secret hooks. Full suite 133 passed, 1 skipped.

## Notes

- **The parser is a dispatch table, not a state machine, for v1.** Each incoming stream-json event has a `type` field; map it to a function. If the state machine temptation hits, resist — it's premature abstraction at this stage.
- **Pin the Claude Code CLI version** in a file like `apps/mars-runtime/.tool-versions` or similar. Upgrading is a deliberate act, not a consequence of `brew update`.
- The in-memory queue is fine for Epic 1 because the supervisor and the "consumer" (control plane proxy) will run in the same process until Epic 2 splits them.
- Do NOT add Codex support here. Epic 3 does that. Adding it now doubles the surface area and risk of this already-risky epic.
- **Camtom reference:** read `services/fastapi/src/products/agents/agent/events.py` lines 18-111 to see the durable/ephemeral pattern before writing `events/types.py`. Copy the shape, not the imports.
