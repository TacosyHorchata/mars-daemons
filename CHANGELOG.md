# Changelog

All notable changes to `mars-daemons` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-04-16

First public release. Enterprise claude code.

### Added
- **HTTP daemon** (`python -m mars_runtime.daemon`) with bearer token auth, SSE streaming, SQLite turn tracking, and crash recovery.
- **Per-user workspaces** with kernel-enforced Unix uid isolation. Workers drop privileges via `setuid`/`setgid`. Bash works — OS contains the blast radius.
- **Multi-agent** in one process: N `AgentConfig`s resolved per session from `user-workspaces/{owner}/agents/` with fallback to `shared/agents/`.
- **File exchange**: `POST /v1/sessions/{id}/files` (upload) and `GET /v1/sessions/{id}/files/{path}` (download), both scoped to user workspace.
- **Ownership model**: `X-Owner-Subject` + `X-Owner-Role` headers from trusted backend. Foreign-owned sessions return the same `404` as unknown sessions (no side-channel).
- **Per-user lock** replaces global turn lock. Different users run in parallel; same user is serialized.
- **Operability**: `GET /healthz`, `GET /readyz`, hard turn timeout (`MARS_TURN_TIMEOUT_S`), cancel endpoint (`POST /v1/sessions/{id}/turns/{turn_id}/cancel`), structured per-turn logs.
- **Replay + reconnect**: durable JSONL event log per session with monotonic `sequence`, `GET /v1/sessions/{id}/events?after=<seq>` for replay then live-tail, synthetic `turn_aborted(daemon_restart)` on crash recovery.
- **Workspace as agent brain**: agents, skills, rules, memory all live on the volume. Admins write to `shared/`; regular users read it. Updates are live.
- Dockerfile ready for production deployment.
- Extensive test suite (MVP + v1.1–v1.3 tests).

### Changed
- Package renamed from `mars-runtime` to `mars-daemons` on PyPI.
- Minimum Python bumped to 3.11.

### Security
- Unix uid isolation is the kernel-enforced boundary between users in the same org. Daemon runs as root; workers never do.
- Cross-org isolation is the container boundary (one container + one volume per org).
- HTTP auth (`bearer` + trusted headers) is a network gate. The tool trust model stays "speed bump, not sandbox" — the agent runs in the user's workspace and can do anything within it.

## [0.2.0] — Prior internal

Pre-public CLI-only runtime. Not released publicly.
