# Epic 2 — Event Forwarding & SSE Topology

**Status:** `[ ]` not started
**Days:** 4 (one full day)
**Depends on:** Epic 1 (needs `MarsEvent` types + supervisor emitting events)
**Downstream blockers:** Epic 3, 4, 5 (nothing UI-facing works without the event pipeline)
**Risk level:** MEDIUM

## Summary

Connect the data plane (mars-runtime machine) to the control plane (mars-control backend) via outbound HTTP event forwarding. Machine POSTs events to control plane, control plane persists + fans out via SSE to browsers. This is the **SSE topology flip** recommended by the plan agent — one SSE hop instead of two, reusing Camtom's `HttpEventSink` + `SSEEventSink` patterns verbatim.

## Context

The naive architecture has mars-control relay SSE from mars-runtime to the browser (two hops, two heartbeats, two reconnect policies, bugs). The plan agent flipped it: the machine is the **producer** that POSTs events outbound to control plane; control plane is the **durable sink + fanout** that browsers subscribe to. This is exactly Camtom's pattern, already implemented at `services/fastapi/src/products/agents/agent/sink.py:33-140` with `HttpEventSink` (lines 33-83) and `SSEEventSink` (lines 85-140).

This epic lifts those patterns almost verbatim and wires them together.

## Scope

### In scope
- `apps/mars-runtime/src/events/forwarder.py` — Outbound HTTP event forwarding. NOT a sink — a forwarder that batches events and POSTs to control plane's ingest endpoint with a signed `X-Event-Secret` header. Lifts `HttpEventSink` from Camtom at `services/fastapi/src/products/agents/agent/sink.py:33-83`.
- `apps/mars-control/backend/src/events/ingest.py` — HTTP POST endpoint that receives events from machines, validates `X-Event-Secret`, persists to SQLite `events` table, fans out to connected browsers.
- `apps/mars-control/backend/src/sse/stream.py` — Browser SSE fanout using Camtom's `SSEEventSink` pattern (lines 85-140) + `_format_sse_event` helper (`services/fastapi/src/products/agents/router.py:322-329`) + heartbeat/idle-timeout constants (lines 52-53) + generator pattern (lines 999-1128).
- `apps/mars-control/backend/src/store/events.py` — SQLite persistence for events (for SSE replay on reconnect). Table: `events(id, session_id, sequence, type, data_json, created_at)`.
- `apps/mars-control/backend/src/api/routes.py` — Adds `GET /sessions/{id}/stream` SSE endpoint for browsers.
- Shared-secret config: `X-EVENT-SECRET` env var on both machine and control plane.

### Out of scope (deferred)
- Authentication of browser SSE connections (Epic 4 adds JWT auth)
- Reconnect with `Last-Event-ID` replay (Epic 5 or later — v1 can accept full stream replay on reconnect)
- Redis-backed fan-out for multi-node control plane (v2 — SQLite + in-process fanout is fine for v1 single-host)

## Acceptance criteria

- [ ] `events/forwarder.py` batches events (up to 100 or 500ms, whichever first) and POSTs to control plane. Includes `X-Event-Secret` header.
- [ ] Forwarder handles control plane being unreachable (retry with exponential backoff, buffer events in-memory up to 1000, drop oldest ephemerals if buffer fills)
- [ ] `events/ingest.py` receives events, validates `X-Event-Secret`, rejects unauthorized requests with 401
- [ ] Ingest persists durable events to SQLite `events` table (ephemeral events are fanned out but not persisted)
- [ ] `sse/stream.py` exposes `async def event_generator(session_id: str, request: Request) -> AsyncIterator[str]` that yields SSE-formatted strings
- [ ] SSE heartbeat `:ping\n\n` every 30s (match Camtom `_SSE_HEARTBEAT_INTERVAL`)
- [ ] SSE idle timeout 5min (match Camtom `_SSE_IDLE_TIMEOUT`)
- [ ] SSE generator polls `request.is_disconnected()` and exits cleanly on browser close
- [ ] `GET /sessions/{id}/stream` endpoint returns `StreamingResponse(media_type="text/event-stream")`
- [ ] Integration test: run mars-runtime locally → deploy a session → connect to control plane `/stream` endpoint via `curl -N` → see events flow in SSE format
- [ ] Kill control plane → machine retries + buffers events → restart control plane → buffered events drain through
- [ ] Kill browser SSE connection → control plane detects disconnect within 30s → machine keeps running

## Critical files

| File | Purpose | Camtom reference |
|---|---|---|
| `apps/mars-runtime/src/events/forwarder.py` | Outbound HTTP forwarder | Lifts `HttpEventSink` from `sink.py:33-83` |
| `apps/mars-control/backend/src/events/ingest.py` | HTTP ingest endpoint + auth | New |
| `apps/mars-control/backend/src/sse/stream.py` | Browser SSE fanout | Lifts `SSEEventSink` from `sink.py:85-140` + helpers from `router.py:322-329,52-53,999-1128` |
| `apps/mars-control/backend/src/store/events.py` | SQLite events table | New |
| `apps/mars-control/backend/src/api/routes.py` | Route registration | Extend existing |

## Dependencies

- **Upstream:** Epic 1 (needs `MarsEvent` types, supervisor emitting events)
- **Downstream:**
  - Epic 3 (Fly deploy): machine needs to POST outbound to a deployed control plane
  - Epic 4 (Web UI): browser needs to consume the SSE endpoint
  - Epic 5 (Multi-session): reconcile logic uses the event stream

## Risks

| Risk | Mitigation |
|---|---|
| Events arrive out of order (network retry + batching) | Each event has a monotonic `sequence` per session. Ingest orders by sequence on write. Consumers handle gaps. |
| Forwarder buffer overflow during long control plane outages | Drop oldest ephemeral events first, never drop durable. Log warning when buffer >80% full. Circuit-break if outage >5 minutes. |
| `X-Event-Secret` leaked via logs | Never log the full secret. Log only a hash prefix (first 8 chars of sha256). |
| SSE keeps holding connections after control plane restart | Match Camtom's idle timeout + disconnect detection patterns exactly. Don't invent a new pattern. |
| SQLite lock contention under load | Write events in batches, not per-event. WAL mode enabled. Single writer thread. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] CI green (including integration test that spans runtime + control)
- [ ] End-to-end manual test: machine → control plane → browser SSE, full round-trip, events visible in real time
- [ ] Failure test: kill control plane mid-session, restart, verify no event loss on durables
- [ ] Docstrings on every public class/function

## Stories

Total: **4 stories**, ~8h budget. Most code is lifted from Camtom's `sink.py` + `router.py` with mechanical renames.

- [ ] **Story 2.1 — `forwarder.py` outbound HTTP (lift from Camtom)** (~2h)
  - *Goal:* Outbound HTTP event forwarder that batches events (up to 100 or 500ms) and POSTs to control plane with `X-Event-Secret` header, lifting `HttpEventSink` from Camtom `sink.py:33-83`.
  - *Files:* `apps/mars-runtime/src/events/forwarder.py`
  - *Done when:* forwarder retries on unreachable control plane and buffers up to 1000 events, dropping oldest ephemerals first

- [ ] **Story 2.2 — `events/ingest.py` + SQLite persistence** (~2h)
  - *Goal:* Control plane HTTP POST endpoint validating `X-Event-Secret`, persisting durable events to SQLite (WAL mode), rejecting unauthorized requests with 401.
  - *Files:* `apps/mars-control/backend/src/events/ingest.py`, `apps/mars-control/backend/src/store/events.py`
  - *Done when:* forwarder POST writes a row to events table; bad secret returns 401

- [ ] **Story 2.3 — `sse/stream.py` browser fanout (lift from Camtom)** (~2h)
  - *Goal:* Browser SSE fanout at `GET /sessions/{id}/stream` with 30s heartbeat + 5min idle timeout, lifting `SSEEventSink` + `_format_sse_event` from Camtom `sink.py:85-140` and `router.py:322-329,52-53,999-1128`.
  - *Files:* `apps/mars-control/backend/src/sse/stream.py`, `apps/mars-control/backend/src/api/routes.py`
  - *Done when:* `curl -N /sessions/{id}/stream` receives SSE-formatted events with heartbeat pings

- [ ] **Story 2.4 — End-to-end integration test** (~2h)
  - *Goal:* Integration test spanning mars-runtime → mars-control → browser SSE that verifies full round-trip and no durable event loss across control plane restart.
  - *Files:* `tests/integration/test_event_pipeline.py`
  - *Done when:* test passes through full runtime → control plane → SSE client round-trip including control plane restart

## Notes

- **The topology flip is the critical architectural decision.** Do not try to re-relay SSE from machine to control plane. Machines are ephemeral producers; control plane is the durable fanout point. This match Camtom's production pattern.
- **Read the Camtom sink.py file before writing a single line of code here.** Specifically lines 33-140 — that's the entire pattern. You will lift ~80% of the code verbatim with mechanical renames.
- **SQLite WAL mode** is a one-liner: `PRAGMA journal_mode=WAL;` on connection. Skip this and you'll hit lock contention under concurrent sessions.
- The `events` table doesn't need indexes in v1 except on `(session_id, sequence)`. Don't premature-optimize.
- Ephemeral events (token streaming chunks) are NOT persisted — they only flow through the in-memory fanout. If a browser reconnects mid-stream, it misses the ephemerals but gets all durables. That's the correct tradeoff.
- **Shared-secret rotation** is out of scope for v1. One secret per deployment, set at machine creation time, stored in `fly secrets`.
