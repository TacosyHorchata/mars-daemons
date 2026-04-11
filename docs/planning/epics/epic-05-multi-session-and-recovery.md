# Epic 5 — Multi-Session & Crash Recovery

**Status:** `[ ]` not started
**Days:** 8 (one full day)
**Depends on:** Epic 1 (supervisor), Epic 3 (deployed Fly machine to restart)
**Downstream blockers:** Epic 7 (dev dogfood smoke tests the multi-session path)
**Risk level:** MEDIUM

## Summary

Make `mars-runtime` robust to concurrent sessions and machine restarts. Multiple daemons can run inside one Fly machine (hard cap 3/VM for v1). When the supervisor process restarts (OOM, deploy, host migration), it reconciles the in-memory `active_sessions` dict against the volume state and marks orphans as "needs restart" — never auto-resumes (prevents double-runs). Control plane cross-checks its state against the machine state on reconnect.

## Context

The plan agent flagged two production risks that converge in this epic:
1. The in-memory `active_sessions` dict dies on machine restart, leaving inconsistent state (volume has working dir, control plane has session row, no process running).
2. Two sessions writing to the same git repo on the volume concurrently (blast radius within a workspace).

Both are solved by: (a) persisting a session handle to the volume, (b) reconciling on supervisor startup, (c) enforcing per-session working directories, (d) capping concurrent sessions per VM.

## Scope

### In scope
- **Concurrent sessions in the supervisor.** The `SessionManager` already supports multiple sessions in memory from Epic 1. This epic hardens it:
  - Hard cap 3 concurrent sessions per VM; `POST /sessions` returns 429 if cap reached
  - Per-session working directory: `/workspace/<session-id>/` (created on spawn, deleted on kill)
  - Per-session log routing: every log line tagged with `session_id` (structured logging via `structlog`)
- **Persistent session handle** written to `/workspace/<session-id>/supervisor_handle.json`:
  - Fields: `session_id`, `agent_name`, `pid`, `status`, `started_at`, `last_heartbeat`, `agent_yaml_path`
  - Written on spawn, updated on heartbeat (every 30s), deleted on clean kill
- **`apps/mars-runtime/src/supervisor_recovery.py`** — Runs on supervisor startup:
  1. Scan `/workspace/*` directories
  2. For each directory, read `supervisor_handle.json`
  3. Check if PID is still alive (`os.kill(pid, 0)`)
  4. If alive: reattach (rebuild in-memory session handle pointing at the running subprocess)
  5. If dead: mark the session as `needs_restart` in control plane, do NOT auto-spawn
- **`apps/mars-control/backend/src/sessions/reconcile.py`** — Runs when control plane detects a machine has reconnected after a gap:
  1. Query machine `GET /sessions` for current state
  2. Compare with control plane's session table
  3. Surface divergence to the UI (session shows as "reconnected" or "needs restart")
  4. No automatic action — user clicks resume or kill
- **UI affordances** (minimal updates to Epic 4 dashboard):
  - Session card shows status: `running`, `needs_restart`, `error`
  - "Resume" button for `needs_restart` sessions
  - Error state shows the last known reason

### Out of scope (deferred)
- Auto-resume on supervisor restart (explicit design decision: no auto-resume, prevents double-runs)
- Horizontal scaling across multiple machines per workspace (v2)
- Session migration between machines (v2)
- Rate-limit enforcement beyond the hard cap (v2)

## Acceptance criteria

- [ ] Spawning 4 sessions on one VM → 4th returns HTTP 429 "max sessions reached"
- [ ] Each session has its own `/workspace/<session-id>/` directory on the Fly volume
- [ ] Session A cannot read/write files in session B's directory (enforced by setting subprocess `cwd`)
- [ ] Log lines include `session_id` in structured format, filterable via `fly logs | grep session_id=abc`
- [ ] `supervisor_handle.json` exists in each session dir while running, with correct fields
- [ ] Kill the supervisor process (`docker kill`) → restart → supervisor scans volume → orphan sessions appear as `needs_restart` in control plane within 10s
- [ ] Graceful shutdown: `SIGTERM` → supervisor kills subprocesses, deletes handles, exits cleanly
- [ ] Control plane reconcile: simulate control plane outage → machine keeps running → control plane restarts → reconcile surfaces any divergence within 30s
- [ ] Clicking "Resume" on a `needs_restart` session → spawns a new subprocess for that agent.yaml → session shows `running`
- [ ] Integration test: 3 sessions running → crash supervisor → restart → all 3 show `needs_restart` → resume all 3 → all 3 running again

## Critical files

| File | Purpose |
|---|---|
| `apps/mars-runtime/src/session/manager.py` | Extend with hard cap + per-session cwd + handle persistence |
| `apps/mars-runtime/src/supervisor_recovery.py` | Volume scan on startup |
| `apps/mars-runtime/src/session/handle.py` (new) | `SessionHandle` dataclass + JSON serde + PID check |
| `apps/mars-control/backend/src/sessions/reconcile.py` | Control plane reconciliation logic |
| `apps/mars-control/backend/src/store/session.py` | Add `status` field to SQLite sessions table |
| `apps/mars-control/frontend/components/dashboard/SessionCard.tsx` | Add status + resume button |

## Dependencies

- **Upstream:** Epic 1 (supervisor), Epic 3 (Fly volume, real machine to crash-test)
- **Downstream:** Epic 7 (dogfood smoke test #2 validates multi-session crash recovery)

## Risks

| Risk | Mitigation |
|---|---|
| PID reuse after reboot (a new unrelated process has the old PID) | Before reattaching, also verify the process command line matches (`/proc/<pid>/cmdline` contains `claude` or `codex`). |
| Race: session spawning while supervisor is shutting down | Guard with a shutdown flag; reject new spawns during shutdown; wait for in-flight spawns to finish before exit. |
| `supervisor_handle.json` corrupted by partial write during crash | Write atomically: write to `.tmp`, fsync, rename. If JSON parse fails on recovery, mark session as `needs_restart` and move on. |
| Reattach to a running subprocess loses stdout buffer (events already emitted but not captured) | Accept: the reattach flags session as "reattached" in the UI with a warning that event history before reattach is incomplete. User can kill + restart for a clean slate. |
| Per-session cwd enforcement bypassed by `..` in agent.yaml paths | Sanitize all paths in `AgentConfig` validation: reject anything with `..` or absolute paths outside `/workspace/<session-id>/`. |
| 3-session hard cap too low for real users | Accept for v1. v1.1 bumps to 10, v2 dynamic based on machine size. Document the limit in `docs/security.md`. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] Integration test passes: 3 concurrent sessions → crash → recover → resume all
- [ ] Hard cap enforced with clear error message
- [ ] Session isolation verified (A can't read B's files)
- [ ] Reconcile works end-to-end with a real machine outage
- [ ] Docstrings on recovery + reconcile logic (subtle code, future-you will thank you)

## Stories

Total: **4 stories**, ~8h budget. NEVER auto-resume — all recovery requires human click on "Resume".

- [x] **Story 5.1 — `SessionHandle` + atomic write + PID check** (~2h)
  - *Goal:* `SessionHandle` dataclass with JSON serde, atomic writes via `.tmp` + fsync + rename, and PID liveness check via `os.kill(pid, 0)` plus cmdline verification.
  - *Files:* `apps/mars-runtime/src/session/handle.py`, `tests/runtime/test_session_handle.py`
  - *Done when:* handle survives partial-write crash and PID check distinguishes alive/dead processes
  - *Outcome:* Shipped as `PersistedSessionHandle` (distinct from the runtime `SessionHandle`). Atomic write via low-level `os.open/write/fsync/close/replace`, parent-dir create, 0600 perms, `.tmp` cleanup. `read_handle` maps every parse failure to `None`. `is_pid_alive` (signal 0 idiom), `find_process_cmdline` (/proc on Linux + `ps` fallback on macOS), `is_claude_or_codex_process` with exact token matching so PID reuse on `claudetronic` / `codexplorer` is caught. `scan_workspace_handles` walks `/workspace/*` sorted, returns `(dir, handle_or_None)` pairs. 30 unit tests. Full suite 422 passed, 1 skipped.

- [x] **Story 5.2 — Hard cap + per-session cwd + structured logging** (~2h)
  - *Goal:* `SessionManager` enforces 3-session cap (429 on overflow), creates per-session `/workspace/<session-id>/` cwd, and binds `session_id` via `structlog` contextvars.
  - *Files:* `apps/mars-runtime/src/session/manager.py`, `apps/mars-runtime/src/supervisor.py`, `tests/runtime/test_session_manager_cap.py`, `tests/runtime/test_session_manager.py` (regression updates), `tests/runtime/test_supervisor_api.py` (+429 test)
  - *Done when:* 4th session POST returns 429, session A cannot read session B's cwd, log lines carry `session_id=`
  - *Outcome:* (1) **Hard cap** — `MAX_SESSIONS_PER_MACHINE=3` constant, `SessionCapReachedError` exception; `SessionManager.spawn` raises when the active dict hits the cap (configurable via `max_sessions=` ctor kwarg). Supervisor's `POST /sessions` maps this to HTTP 429 with the full error message so the UI can show "machine full, wait". Cap releases on `kill` so the 4th spawn succeeds after one session dies. Two existing zombie-stress tests updated with `max_sessions=20` to pass the new cap. (2) **Per-session cwd** — `session_workdir(session_id)` returns `<workspace_root>/<session_id>`, `spawn` creates it before spawning and stores it in `handle.metadata["session_workdir"]`. `workspace_root` is injectable via constructor (tests pass `tmp_path`). Graceful degradation: on local dev where `/workspace` is a read-only path, `OSError` during mkdir is caught, a warning is logged, and the session spawns without an isolated cwd (session isolation only guaranteed when the root is writable — documented). (3) **Session-tagged logging** — zero new deps. `current_session_id: ContextVar[str | None]` lives in `session.manager`; `SessionIdLogFilter` is a `logging.Filter` that stamps `record.session_id` from the context var (defaulting to `"-"` when unset) so format strings referencing `%(session_id)s` never `KeyError`. `install_session_log_filter(logger)` helper for app wiring. 19 unit tests: cap default/custom/overflow/allows-after-kill/invalid-ctor, per-session workdir creation/uniqueness/helper-purity, contextvar defaults/filter stamping/dash-fallback/task-isolation/installer. Full suite: 437 passed, 1 skipped. 27/47 stories done.

- [x] **Story 5.3 — ★ `supervisor_recovery.py` startup scan** (~2h)
  - *Goal:* Supervisor startup scans `/workspace/*`, reads each `supervisor_handle.json`, marks dead-PID sessions as `needs_restart` (no auto-spawn), reattaches live PIDs after cmdline verification.
  - *Files:* `apps/mars-runtime/src/supervisor_recovery.py`, `tests/runtime/test_supervisor_recovery.py`
  - *Done when:* crash + restart with 3 running sessions → all 3 appear as `needs_restart` in control plane within 10s
  - *Outcome:* Pure-function scanner + classifier that returns `list[RecoveredSession]` without mutating anything or spawning subprocesses. `RecoveryStatus` enum covers 4 cases, each with its own human-readable reason: `dead` (pid not running), `orphan_alive` (pid alive but cmdline not claude/codex — PID-reuse-after-reboot), `reattach_candidate` (pid alive AND running claude/codex — v1 does **not** auto-reattach, admin must decide whether to kill + restart), `corrupt_handle` (handle file unparseable). Every status has `needs_restart=True` — the UI uses that as the sole gate for the Resume button. `classify_session` takes injectable `is_alive` / `is_claude_or_codex` callables so tests never fork real processes. `recover_workspace(root)` iterates `scan_workspace_handles` (Story 5.1), classifies each, logs at the right level (INFO for dead, WARNING for the rest), returns sorted results. Epic's "NEVER auto-resume" rule is enforced structurally — there is no code path in this module that spawns. 15 unit tests covering all four statuses, the `session_id` property, empty / missing root, mixed outcomes (one of each status in one scan), and log-level verification per status. Full suite: 452 passed, 1 skipped. 28/47 stories done.

- [ ] **Story 5.4 — Control plane reconcile + UI status** (~2h)
  - *Goal:* Control plane `reconcile.py` cross-checks machine sessions vs DB on reconnect + `SessionCard` shows `running` / `needs_restart` / `error` status with Resume button.
  - *Files:* `apps/mars-control/backend/src/sessions/reconcile.py`, `apps/mars-control/backend/src/store/session.py`, `apps/mars-control/frontend/components/dashboard/SessionCard.tsx`
  - *Done when:* clicking Resume on a `needs_restart` session spawns a new subprocess and the card flips to `running`

## Notes

- **NEVER auto-resume.** The double-run scenario (two `claude` processes both writing to the same work dir) is catastrophic. Always require human confirmation via the Resume button.
- **PID checking** via `os.kill(pid, 0)` is the portable Python pattern. No exception = process exists. `ProcessLookupError` = dead.
- **Log tagging:** `structlog` with `contextvars` binding. Every log call inside a session handler automatically includes `session_id`. Avoid manual threading.
- **Reconciliation is eventually consistent.** The UI may show a stale status for up to 30 seconds during reconnect. That's fine — the user is already confused (why is this session weird?), a few seconds of ambiguity is acceptable.
- **Testing this epic is hard** because you need to actually restart containers. Set up a local docker-compose test harness where you can `docker kill` the supervisor and restart it with volumes intact.
- **The 3-session cap** is a soft architectural choice. It's easy to change later. Don't let anyone rathole on "why 3?" — the answer is "it's a v1 number, we'll revisit with data."
