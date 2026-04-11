# Mars repo split — `mars-daemons` (OSS core) + `mars-control` (product)

**Status:** draft — awaiting Pedro's approval before any destructive git op
**Created:** 2026-04-11
**Author:** Claude + Pedro, continuous backlog cycle

## Decision

Split `apps/mars-control/` out of `mars-daemons` into a **separate private
repo**. `mars-daemons` becomes a vendor-agnostic OSS project. `mars-control`
becomes the commercial product that runs on top of it.

### Principle — core is agnostic

Nothing in `mars-daemons` may mention Camtom, Orion, Maat, customs, Zoho,
WhatsApp-for-operators, or any customer vertical. The core only knows:

- how to parse and validate an `agent.yaml`
- how to spawn `claude` / `codex` CLIs inside a sandbox
- how to parse stream-json output into Mars events
- how to forward events outbound via HTTP
- how to manage sessions + recovery + memory
- how to edit `CLAUDE.md` with supervisor restart
- how to run locally (`mars run --local`)
- how to deploy to a Fly machine (`mars deploy`)
- how to SSH into a deployed machine (`mars ssh`)

Any concept that requires "who is the operator, what templates do we show
them, what's the onboarding flow, how do they auth, how do they pay" is
**product** — that lives in `mars-control`.

## Why this split (force-ranked)

1. **OSS core = distribution channel.** Every AI-eng team asking "how do I
   run Claude Code sandboxed?" should find `mars-daemons` on GitHub. Same
   mechanic as Pilot's 100-star target. This is the physical artifact the
   tsunami thesis needs.
2. **Independent release cadence.** Core must be stable for every consumer.
   Control plane evolves fast per customer. Coupling them means the core
   version drags at the pace of the slowest product customer.
3. **Threat model separation.** Core runs untrusted code (that's its job).
   Control plane holds auth, secrets, billing. Different blast radius,
   different audit surfaces — separate repos enforce the boundary.
4. **Natural upgrade funnel.** Whoever adopts OSS core locally eventually
   wants the wizard + chat + auth. `mars-control` is the organic upsell to
   users who already depend on the core. Pulumi, Grafana, Supabase,
   Temporal — all ran this playbook.
5. **The "Próximamente" wizard placeholder disappears as a problem.**
   Because the wizard is no longer v1 of the harness; it's v1 of a
   *different product* with its own timeline and its own customers.

## Contamination audit — what must be cleaned before split

Run on 2026-04-11 against `main` @ commit `3136460`.

### Files in core that reference Camtom-world concepts

| File | Nature | Action |
|---|---|---|
| `apps/mars-runtime/src/events/types.py` | Docstring attribution: "shape mirrors Camtom's pattern at services/fastapi/..." | Rewrite: drop Camtom reference, explain the shape on its own merits |
| `apps/mars-runtime/src/events/forwarder.py` | Docstring: "Lifts the basic shape of Camtom's `HttpEventSink`..." | Rewrite: describe the sink on its own terms |
| `apps/mars-runtime/src/session/claude_code_stream.py` | Comment: "matches Camtom's `turn_completed` + `turn_error` pair, except Mars keeps..." | Rewrite: drop the comparison, describe Mars behavior directly |
| `examples/orion-daemon.yaml` | Entire file is Orion/Maat/Zoho-specific ("Ops assistant for Orion's tracker fleet", Zoho MCP, `whatsapp` MCP) | **Move to `mars-control/examples/`** — this was always a customer example, never a generic one |

Nothing in `packages/mars-cli/` needed cleaning — already agnostic.

### Non-contamination but worth tightening before split

- `mars_control.db`, `mars_control.db-shm`, `mars_control.db-wal` exist in
  the repo root from local dev. Not tracked in git (✓). Add explicit
  `mars_control.db*` to `.gitignore` as belt-and-suspenders before the
  split, so these never accidentally leak into either repo.
- `README.md` needs a rewrite post-split. Core README should describe the
  harness only. Control plane README is a separate doc.

## File-by-file split plan

### Stays in `mars-daemons` (OSS core)

```
apps/mars-runtime/                # supervisor, parser, session mgr, event pipeline
packages/mars-cli/                # `mars` CLI (run, deploy, ssh, edit-prompt, memory)
examples/                         # generic reference agents only
  pr-reviewer-agent.yaml          # dev-track dogfood — generic, self-referential
  (hello-agent.yaml to be added)  # simplest possible example for docs
schemas/                          # (to be created) agent.yaml JSON schema
docs/
  architecture.md                 # harness architecture (P0 for ship pack)
  getting-started-dev.md          # local mode, CLI usage (P0 for ship pack)
  security.md                     # threat model — core half only
  split-plan.md                   # this doc (moves to mars-daemons history)
  planning/                       # v1 plan, epic trackers (core-only epics)
  decisions/                      # ADRs relevant to core
spikes/                           # spike artifacts from Epic 0
tests/                            # integration tests that only touch core
pyproject.toml                    # core Python deps
pnpm-lock.yaml                    # keep for now (drop if no JS in core post-split)
Dockerfile.runtime                # (rename from current) runtime image
.github/workflows/                # core CI only
LICENSE                           # TBD — Apache 2.0 is my default
README.md                         # rewritten as "Mars Daemons — the harness"
```

### Moves to `mars-control` (private, new repo)

```
apps/mars-control/backend/        → backend/
apps/mars-control/frontend/       → frontend/
apps/mars-control/templates/      → templates/
  tracker-ops-assistant.yaml      # Camtom-Maat specific
  tracker-ops-assistant.prompt.md # Spanish system prompt
examples/orion-daemon.yaml        → examples/orion-daemon.yaml
                                    (customer-specific, belongs here)
tests/ (control plane subset)     → tests/
docs/planning/epics/              → docs/planning/epics/
  epic-04-*.md (web UI)           # product-scoped epics
  epic-08-*.md (operator turnkey)
  epic-09-*.md (security + launch, control plane half)
v1.1-backlog.md (P0 #1, #4, #5)   # items scoped to control plane
pyproject.toml (control subset)   # FastAPI, auth deps
package.json                      # Next.js + frontend deps
README.md                         # "Mars Control — the product on top of mars-daemons"
```

### Deleted in both

```
mars_control.db*                  # SQLite dev artifacts — .gitignore them
node_modules/                     # regenerated
.venv/                            # regenerated
```

## Git subtree split procedure

Exact commands to run from `/Users/pedrorios/Desktop/mars-daemons` after
the contamination cleanup commits are in:

```bash
# 1. Ensure clean working tree, on main, tests green
cd /Users/pedrorios/Desktop/mars-daemons
git status                         # must be clean
python -m pytest 2>&1 | tail -3    # must be 516+ passed

# 2. Subtree split — creates a new branch containing ONLY apps/mars-control/ history
git subtree split --prefix=apps/mars-control -b mars-control-split

# 3. Create the new repo locally (sibling directory, git-wise isolated)
mkdir ../mars-control
cd ../mars-control
git init
git pull ../mars-daemons mars-control-split
# → mars-control now has only the apps/mars-control/ subtree with full history

# 4. Copy over non-subtree files that belong to control plane but lived elsewhere
#    (examples/orion-daemon.yaml, relevant docs, relevant tests)
#    Use git mv from mars-daemons first to preserve history, then subtree split wouldn't
#    have them — so instead: cp + git add in mars-control, git rm in mars-daemons.
#    Acceptable history loss for non-code artifacts.

# 5. Restructure mars-control/
#    - Move apps/mars-control/backend/ → backend/
#    - Move apps/mars-control/frontend/ → frontend/
#    - Move apps/mars-control/templates/ → templates/
#    - Write new README, pyproject.toml, package.json
git add -A
git commit -m "chore: flatten layout from apps/mars-control/ to repo root"

# 6. Run control-plane tests in new repo
python -m pytest        # must be green

# 7. Back in mars-daemons: remove the old apps/mars-control/ directory
cd ../mars-daemons
git rm -r apps/mars-control/
git rm examples/orion-daemon.yaml
git commit -m "chore(split): remove control plane — moved to mars-control repo"

# 8. Run core tests in mars-daemons
python -m pytest        # must be green — expected ~300-400 tests, down from 516

# 9. Update README.md in both repos

# 10. Push mars-daemons to its remote (already exists)
git push

# 11. Create mars-control remote (private repo), push first commit
#     GitHub / Fly / Vercel repo setup is a separate step Pedro owns
```

**Reversibility:** steps 1-6 are fully reversible. The destructive moment
is step 7 (`git rm -r apps/mars-control/` in mars-daemons). Until that
commit is pushed, both repos coexist safely and `mars-daemons` can be
restored by reverting one commit.

## Test suite reorganization

Currently: **516 passed, 1 skipped** (2026-04-11, commit `3136460`).

### Expected split

- **`mars-daemons` post-split:** ~300-400 tests. Supervisor, parser,
  session manager, event forwarding, CLI, runtime integration.
- **`mars-control` post-split:** ~120-200 tests. Auth, magic-link, SSE
  fanout, templates endpoint, wizard backend, session locator.

### Integration tests that touch both layers

The current suite has tests that spin up a control plane + point at a
supervisor via `MARS_LOCAL_SUPERVISOR_URL`. Those tests are **control plane
tests** — they verify the control plane correctly proxies to a supervisor.
They go with `mars-control` and use an httpx `MockTransport` or a real
runtime via `pip install mars-daemons` in CI.

This implies `mars-control`'s CI installs `mars-daemons` as a dev
dependency, which implies `mars-daemons` must be publishable (see open
questions).

## v1.1 backlog re-prioritized by repo

Applied to the current `docs/v1.1-backlog.md` P0 list:

| v1.1 P0 item | Repo | Notes |
|---|---|---|
| #1 Anthropic OAuth (Story 8.4 steps 4-6) | `mars-control` | Wizard steps + OAuth + secrets form + deploy endpoint all live in the product repo |
| #2 Live Fly deploy + spikes 4 & 5 | **split** | `mars-daemons` owns the Dockerfile + `mars deploy` CLI; `mars-control` owns the deploy-target config (its own Fly app) |
| #3 Dev-track dogfood (Epic 7) | `mars-daemons` | `pr-reviewer-agent.yaml` runs the core, no control plane needed |
| #4 Maat setup call (Story 9.3) | `mars-control` | Product research, feeds the wizard UX |
| #5 Mobile real-phone E2E (Story 8.5) | `mars-control` | UI is in the product |

P1/P2 items get re-sorted by owning repo at v1.1 kickoff, not now.

## Open questions — Pedro must decide

### 1. License for `mars-daemons`

- **Apache 2.0** (my default recommendation) — permissive, patent grant,
  standard for infra-layer OSS (Kubernetes, Pulumi, Temporal). Max
  adoption.
- **MIT** — even more permissive but no patent grant. Fine for smaller
  projects, less-common for infra.
- **AGPL** — copyleft; forces downstream SaaS to open-source their control
  plane. Protects against a competitor forking the core into their own
  hosted product. But scares some adopters.
- **BSL (Business Source License)** with conversion to Apache after 4
  years — Sentry/CockroachDB/Hashicorp model. Prevents direct competition
  early, goes fully open later. Most founder-friendly.

**My recommendation:** Apache 2.0. The commercial moat is `mars-control`
(the product experience + Anthropic OAuth + customer-curated templates +
proprietary data), not the harness. OSS-ing the harness aggressively is
what creates the distribution wedge. BSL is defensible but adds friction
that kills adoption.

### 2. Name of the new repo

Options:
- `mars-control` (matches current internal naming — `apps/mars-control/`)
- `mars-cloud` (hints at hosted/SaaS, less accurate since it's self-hosted too)
- `mars-platform` (too generic)
- `mars-saas` (too on-the-nose)

**My recommendation:** `mars-control`. Zero renaming, consistent with
existing code paths, the word "control" already implies "plane" to anyone
who's touched infra.

### 3. Visibility day 1

- `mars-daemons`: **public** on GitHub from the split commit. Even if it's
  rough, visible commits signal momentum and seed the eventual star push.
- `mars-control`: **private** on GitHub until there's at least one paying
  or committed design-partner customer. No reason to expose product
  internals before then.

### 4. PyPI / npm publish timing

- `mars-daemons` as `pip install mars-daemons` → needs to happen before
  `mars-control`'s CI can depend on it. So: publish early alpha
  (`mars-daemons==0.1.0a1`) the same day as the split, even if nobody's
  installing it yet. Future-us will thank present-us.
- `@mars/cli` on npm → defer. No JS consumers yet.

## Rollback plan

If anything goes wrong during steps 1-6 of the split procedure, both
repos are still in a reversible state. The irreversible moment is
step 7 — `git rm -r apps/mars-control/` followed by a push.

Before that irreversible step:

- Confirm `mars-control` tests are green
- Confirm `mars-daemons` tests would still be green with the directory
  removed (run `pytest` from a fresh clone or with the directory
  renamed first to simulate)
- Back up `mars-daemons` at the pre-split commit (`git tag
  pre-split-backup 3136460`)

If a regression surfaces after the split is pushed:

- Revert the split commit in `mars-daemons` (the `git rm -r` commit)
- `mars-control` stays as-is; you've just gained redundancy, not lost
  anything

## Timeline (concrete, no hedging)

After Pedro approves this plan:

1. Contamination cleanup (docstrings + `.gitignore`) — 1 commit
2. Pre-split backup tag — 1 git tag
3. Subtree split + new repo creation — 1 commit in each repo
4. Flatten control plane layout in new repo — 1 commit
5. Test verification both sides
6. Remove `apps/mars-control/` + `examples/orion-daemon.yaml` from core — 1 commit
7. Final READMEs — 1 commit each
8. Push `mars-daemons` to existing remote
9. Create `mars-control` remote on GitHub (Pedro owns this step), push

From the moment Pedro says "GO" to both test suites green and ready to
push: this is a single continuous session. Commits are atomic and
reversible individually.

## Not in scope for this split

- Renaming the `mars` binary or any package names (the CLI stays `mars`)
- Changing the HTTP API between control plane and supervisor (still
  `X-Event-Secret` outbound posts, still session proxy routes)
- Adding new features to either side
- License changes beyond picking one

These are deliberately out of scope so the split is the *only* variable.
Feature work resumes on both repos the day after.
