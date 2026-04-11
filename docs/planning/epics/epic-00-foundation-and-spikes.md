# Epic 0 — Foundation & Spikes

**Status:** `[ ]` not started
**Days:** 1 AM (spikes 1+2) → 1 PM (agent.yaml + scaffold) → 2 AM (spike 3) → 2 PM (supervisor skeleton begins, rolls into Epic 1)
**Depends on:** nothing (entry point)
**Downstream blockers:** EVERYTHING — this epic gates all other work
**Risk level:** HIGH (unknowns must be validated)

## Summary

Validate the three hardest external unknowns (Claude Code headless OAuth, `stream-json` schema, permission round-trip) in parallel with creating the repo, defining the `agent.yaml` schema, and writing the first two example agent files. This epic is non-negotiable foundation — if spikes 1 or 2 fail, the architecture pivots before Epic 1 starts.

## Context

The plan agent identified three unknowns that must be validated before committing to 10 days of build work:
1. Can `claude setup-token` + `CLAUDE_CODE_OAUTH_TOKEN` actually run Claude Code headlessly in a Docker container without a browser?
2. What is the exact JSONL schema of `claude -p --output-format stream-json`, and is it stable enough to parse?
3. Can we intercept permission prompts and inject responses back into the running Claude Code session?

Pedro chose parallel spikes (not a dedicated Day 0), so these run alongside repo scaffold + schema work. If any spike fails, the bounded pivots are documented in the plan.

## Scope

### In scope
- Create new repo `github.com/tacosyhorchata/mars-daemons` with monorepo layout (apps/, packages/, tests/, docs/, examples/)
- Monorepo tooling decision (pnpm workspace? uv workspace? bare Python + Node?)
- Spike 1: Claude Code headless OAuth in local Docker (2h)
- Spike 2: stream-json schema capture → canonical fixture file (1h)
- Spike 3: Permission prompt round-trip (2h)
- `mars-control/backend/src/schema/agent.py` — Pydantic `AgentConfig` class
- `packages/mars-cli/src/mars/__main__.py` — CLI entrypoint
- `packages/mars-cli/src/mars/init.py` — `mars init` command that scaffolds an `agent.yaml` in the current directory
- `examples/pr-reviewer-agent.yaml` — Pedro's first dogfood daemon definition
- `examples/orion-daemon.yaml` — Reference for Maat's eventual template
- `tests/contract/fixtures/stream_json_sample.jsonl` — captured from spike 2, basis for Epic 1
- Basic CI: GitHub Actions that runs `uv sync` + `pytest` + `pnpm install` + `pnpm typecheck` on PR

### Out of scope (deferred)
- Any supervisor runtime code (Epic 1)
- Any deploy logic (Epic 3)
- Any UI (Epic 4)
- The `tracker-ops-assistant.yaml` template (Epic 8) — the two example files here are dev-track only

## Acceptance criteria

- [ ] Repo exists at `github.com/tacosyhorchata/mars-daemons` (private is fine for v1)
- [ ] `README.md` has a one-paragraph pitch and the repo structure
- [ ] `apps/mars-control/backend/src/schema/agent.py` defines `AgentConfig` with all fields (`name`, `description`, `runtime`, `system_prompt_path`, `mcps[]`, `env[]`, `tools[]`, `workdir`)
- [ ] `AgentConfig.parse_file('examples/pr-reviewer-agent.yaml')` succeeds
- [ ] `AgentConfig.parse_file('examples/orion-daemon.yaml')` succeeds
- [ ] Running `mars init` in an empty directory creates a valid starter `agent.yaml`
- [ ] **Spike 1:** a shell script in `spikes/01-claude-code-oauth.sh` documents the working OAuth flow OR a written pivot decision to BYO-API-key is committed to `docs/decisions/001-oauth-pivot.md`
- [ ] **Spike 2:** `tests/contract/fixtures/stream_json_sample.jsonl` exists and contains one complete session (system_init → assistant → tool_call → tool_result → result)
- [ ] **Spike 3:** `spikes/03-permission-roundtrip.md` documents whether stdin injection works or we must use `--permission-mode acceptEdits` fallback
- [ ] CI runs on PR and passes on an empty scaffold
- [ ] Hard gate: before starting Epic 1, spikes 1 and 2 are green or pivots are documented

## Critical files

| File | Purpose |
|---|---|
| `apps/mars-control/backend/src/schema/agent.py` | Pydantic `AgentConfig` — single concrete class, no Protocol yet |
| `packages/mars-cli/src/mars/init.py` | `mars init` scaffolder |
| `packages/mars-cli/src/mars/__main__.py` | CLI entrypoint (Click) |
| `examples/pr-reviewer-agent.yaml` | Pedro's first daemon spec |
| `examples/orion-daemon.yaml` | Reference for operator use case |
| `spikes/01-claude-code-oauth.sh` | Reproducible OAuth spike |
| `spikes/02-stream-json-capture.sh` | Reproducible schema capture |
| `spikes/03-permission-roundtrip.md` | Permission round-trip findings |
| `tests/contract/fixtures/stream_json_sample.jsonl` | Fixture for Epic 1 parser |
| `.github/workflows/ci.yml` | Basic PR checks |

## Dependencies

- **Upstream (must be done first):** none
- **Downstream (waits for this):** all other epics; Epic 1 specifically needs spike 2 fixture

## Risks

| Risk | Mitigation |
|---|---|
| Spike 1 fails (headless OAuth broken in Docker) | Pivot to BYO-API-key mode. Document in `docs/decisions/001-oauth-pivot.md`. Architecture unchanged, marketing adjusted. No day lost. |
| Spike 2 output changes between Claude Code versions | Pin exact Claude Code version in spike script; record version hash in the fixture file header |
| Spike 3 shows stdin injection impossible | Fallback to `--permission-mode acceptEdits` + denylist hook. v1 has reduced human-in-loop but still ships. |
| Monorepo tooling rabbit hole (pnpm vs uv vs bare) | Default: **uv workspace for Python, pnpm workspace for Node, nothing fancy.** Do not spend more than 1 hour on tooling. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] CI green on `main`
- [ ] All acceptance criteria checked
- [ ] Decisions from spikes documented in `docs/decisions/`
- [ ] Next session can clone repo, read README, and understand what to do next

## Stories

Total: **6 stories**, ~9h budget. Epic 0 gates all other epics — spikes 1 and 2 are hard prerequisites for Epic 1.

- [x] **Story 0.1 — Repo scaffold + CI** (~1h)
  - *Goal:* Monorepo layout (apps/, packages/, tests/, docs/, examples/, spikes/) with GitHub Actions CI running Python + Node checks on PR.
  - *Files:* `.github/workflows/ci.yml`, `README.md`, `pyproject.toml`
  - *Done when:* empty scaffold merged to main with CI green

- [x] **Story 0.2 — `AgentConfig` schema + 2 example files** (~2h)
  - *Goal:* Pydantic `AgentConfig` with all v1 fields (`name`, `description`, `runtime`, `system_prompt_path`, `mcps`, `env`, `tools`, `workdir`) + two valid example agent.yaml files.
  - *Files:* `apps/mars-control/backend/src/schema/agent.py`, `examples/pr-reviewer-agent.yaml`, `examples/orion-daemon.yaml`
  - *Done when:* `AgentConfig.parse_file()` succeeds on both examples with unit test

- [x] **Story 0.3 — `mars init` CLI command** (~1h)
  - *Goal:* CLI subcommand that scaffolds a starter agent.yaml in the current directory.
  - *Files:* `packages/mars-cli/src/mars/__main__.py`, `packages/mars-cli/src/mars/init.py`
  - *Done when:* `mars init` in an empty dir creates a valid agent.yaml parseable by `AgentConfig`

- [x] **Story 0.4 — ★ Spike 1: Claude Code headless OAuth** (~2h)
  - *Goal:* Prove `claude setup-token` + `CLAUDE_CODE_OAUTH_TOKEN` runs Claude Code headlessly in a Docker container, OR commit a BYO-API-key pivot decision.
  - *Files:* `spikes/01-claude-code-oauth.sh`, `docs/decisions/001-oauth-status.md`
  - *Done when:* headless `claude -p` runs in a container OR pivot decision committed
  - *Outcome:* host stream-json headless validated (clean-env `claude -p` produces well-formed JSONL with `system.init → assistant → result` events). Container + setup-token path scripted in `spikes/01-claude-code-oauth.sh` but pending Pedro's interactive browser OAuth. Architecture commitment is auth-mode-agnostic — safe to proceed. See `docs/decisions/001-oauth-status.md`.

- [x] **Story 0.5 — ★ Spike 2: stream-json schema capture** (~1h)
  - *Goal:* Capture canonical Claude Code stream-json output as a test fixture for Epic 1's parser.
  - *Files:* `spikes/02-stream-json-capture.sh`, `tests/contract/fixtures/stream_json_sample.jsonl`
  - *Done when:* fixture contains one complete session with system_init → assistant → tool_call → tool_result → result events
  - *Outcome:* 6-line fixture captured from pinned Claude Code 2.1.101: `system.init → assistant(tool_use) → rate_limit_event → user(tool_result) → assistant(text) → result.success`. Spike script drops user-global hook noise + strips cmux `[rerun: bN]` artifacts so the fixture matches what a clean Mars Fly container will emit.

- [ ] **Story 0.6 — Spike 3: Permission round-trip** (~2h)
  - *Goal:* Determine if Claude Code permission prompts can be intercepted + responded to programmatically, or confirm `--permission-mode acceptEdits` fallback.
  - *Files:* `spikes/03-permission-roundtrip.md`
  - *Done when:* working mechanism documented OR `acceptEdits` fallback decision committed

## Notes

- The `agent.yaml` schema is intentionally concrete (no Protocol, no inheritance) for v1. Over-engineering here wastes time.
- The `spikes/` directory should stay in the repo as historical artifacts — future contributors will thank you.
- Default runtime in `AgentConfig` should be `claude-code`. `codex` is added in Epic 3.
- Do NOT create the `tracker-ops-assistant.yaml` template here. That's Epic 8's deliverable.
