# Epic 7 — Developer Dogfood (Pedro)

**Status:** `[ ]` not started
**Days:** 10 (one full day of integration testing + bug fixes)
**Depends on:** Epics 1, 2, 3, 4, 5, 6 (the entire developer track)
**Downstream blockers:** Epic 9 (launch blocked until dogfood smoke tests pass)
**Risk level:** LOW (but absorbs latent bugs from earlier epics)

## Summary

Pedro deploys a real PR-reviewer daemon against his own `epic/agents-v2` branch, uses it for a day, and fixes every bug he hits. This is the end-to-end integration test of the developer track and the first moment Mars is actually used for productive work. Smoke tests 1–7 from the v1 plan are exercised and verified.

## Context

This epic exists because no amount of unit tests matches the signal from the founder using the product in anger. Pedro's PR reviewer daemon watches `epic/agents-v2`, reads new commits, and posts reviews as chat messages. It's the cleanest possible first use case: scoped, useful, bounded, and dogfood-worthy.

The goal isn't to ship a polished PR reviewer — it's to *prove Mars works end-to-end* by shipping the smallest useful real thing.

## Scope

### In scope
- **`examples/pr-reviewer-agent.yaml`** (finalized — the draft from Epic 0 may need updates after learning from Epics 1–6)
- **System prompt in `pr-reviewer-prompt.md`** — instructs the daemon to: pull latest `epic/agents-v2`, compare last N commits, identify smells/regressions/missing tests, post findings as a chat message
- **Deploy it:** `mars deploy examples/pr-reviewer-agent.yaml` → get URL → bookmark
- **Use it for real:** chat "review the latest commit on epic/agents-v2" → verify it works end-to-end
- **Run smoke tests 1–7** from the v1 plan:
  1. PR reviewer daemon — deploy, chat, tool use, laptop-close survival, SSH access
  2. Multi-session on one VM — 3 daemons, dashboard, isolation, crash recovery
  3. Local mode — `mars run --local` with the same agent.yaml
  4. CLAUDE.md immutability — attempt edit → blocked → admin edit → restart
  5. Secrets speed bump — deploy with `GITHUB_TOKEN`, verify agent can use it but cannot `echo $` it
  6. Memory capture — 30-minute session → `mars memory export` → verify bundle
  7. Magic-link signup — fresh email → end-to-end auth flow
- **Bug fix pass** — every issue found during real use is filed + fixed within the day. Triage ruthlessly: critical bugs only, polish is Epic 9.
- **Capture real observations** in `docs/dogfood-notes.md` — Pedro's raw impressions, what felt right, what felt wrong, what's missing. This is the input to v1.1 backlog.

### Out of scope (deferred)
- The Maat operator track (Epic 8)
- Security hardening beyond bug fixes (Epic 9)
- v1.1 features requested during dogfood (capture in notes, don't build)
- Polish, UI refinement, design improvements (Epic 9 or v1.1)

## Acceptance criteria

- [ ] **Smoke test 1:** PR reviewer deployed, chat URL bookmarked, reviewed at least 2 real commits, daemon survived laptop-close for 1+ hour, `mars ssh pr-reviewer` works
- [ ] **Smoke test 2:** 3 different daemons deployed on one workspace, dashboard shows all 3 with distinct name + description, chatting with each doesn't bleed state, crash recovery confirmed (killed machine → `needs_restart` → resumed cleanly)
- [ ] **Smoke test 3:** `mars run --local examples/pr-reviewer-agent.yaml` works on Pedro's laptop, same agent.yaml, terminal I/O
- [ ] **Smoke test 4:** Agent's attempt to write CLAUDE.md blocked with a clear error; admin edit via web UI + CLI both work; session restarts cleanly
- [ ] **Smoke test 5:** `ZOHO_API_KEY` secret deployed (even if just a placeholder), agent can't reveal it via `echo $ZOHO_API_KEY` (PreToolUse blocks)
- [ ] **Smoke test 6:** After 30 min of activity, `mars memory export pr-reviewer` produces a bundle with session history, tool logs, CLAUDE.md diff proposals (if any)
- [ ] **Smoke test 7:** New email signup → magic link → dashboard, full round-trip, cookies persist
- [ ] `docs/dogfood-notes.md` committed with Pedro's real impressions
- [ ] All critical bugs fixed (no crashes, no data loss, no silent failures)
- [ ] Non-critical bugs logged in GitHub issues tagged `v1.1`

## Critical files

| File | Purpose |
|---|---|
| `examples/pr-reviewer-agent.yaml` | The first production daemon |
| `examples/pr-reviewer-prompt.md` | System prompt for the PR reviewer |
| `docs/dogfood-notes.md` | Pedro's raw observations from day-of-use |
| GitHub issues tagged `v1.1` | Captured non-critical bugs and feature requests |

## Dependencies

- **Upstream:** Epics 1, 2, 3, 4, 5, 6 (every developer-track feature)
- **Downstream:** Epic 9 (launch)

## Risks

| Risk | Mitigation |
|---|---|
| Critical bug discovered on Day 10 that requires re-architecture | Accept ship slip of 1–2 days. Dogfood day exists precisely to catch this before Maat sees it. |
| Pedro's PR reviewer use case is too narrow to stress the product | Pedro should ALSO manually run one other daemon (e.g. "summarize today's Camtom production alerts") as a second data point. |
| PreToolUse hook blocks something legitimate that the PR reviewer needs | Iterate on the hook rules — probably need to allow `Edit` on files inside the working dir but block on CLAUDE.md/AGENTS.md specifically. Verify the matcher syntax. |
| Pedro discovers the chat UI is too ugly to use | Accept. Uglier UI ships than a later UI that doesn't ship. Log under v1.1. |
| S3 credentials not set → memory sync fails silently | Add a startup health check: supervisor logs ERROR if S3 credentials missing and memory sync is enabled. Don't crash, but be loud. |
| `mars deploy` produces a URL that 404s | Most likely: control plane's `/sessions/{id}/stream` endpoint isn't wired yet. Dogfood will catch this — trace the full deploy → URL → SSE path carefully. |

## Definition of Done

- [ ] All 7 smoke tests pass
- [ ] `dogfood-notes.md` written with real observations
- [ ] Zero critical bugs open
- [ ] All non-critical bugs filed as GitHub issues
- [ ] Pedro explicitly signs off: "I would use this product tomorrow morning on its own merits"

## Stories

Total: **3 stories**, ~8h budget. Almost no new code — the epic is integration testing + bug fixes against earlier epics.

- [ ] **Story 7.1 — Deploy PR reviewer + smoke tests 1–3** (~3h)
  - *Goal:* Finalize `pr-reviewer-agent.yaml` + system prompt, deploy to Fly, run smoke tests 1 (chat + tool use + laptop-close survival + `mars ssh`), 2 (3-session isolation + crash recovery), 3 (local mode).
  - *Files:* `examples/pr-reviewer-agent.yaml`, `examples/pr-reviewer-prompt.md`
  - *Done when:* all 3 smoke tests pass end-to-end, including 1+ hour laptop-close survival

- [ ] **Story 7.2 — Smoke tests 4–7 (security, memory, auth)** (~2h)
  - *Goal:* Verify CLAUDE.md immutability + admin edit flow, `echo $SECRET` blocked, 30-min memory export bundle exists, fresh-email magic-link signup round-trip.
  - *Files:* *(exercises Epics 4 and 6 — no new files)*
  - *Done when:* all 4 smoke tests pass and memory bundle is inspectable

- [ ] **Story 7.3 — Bug fix pass + dogfood notes + backlog capture** (~3h)
  - *Goal:* Fix every critical bug hit during real use, capture raw observations, file non-critical issues tagged `v1.1`.
  - *Files:* `docs/dogfood-notes.md`
  - *Done when:* Pedro signs off: "I would use this product tomorrow morning on its own merits"

## Notes

- **Dogfood is the moment of truth.** Every prior epic's bugs surface here. Budget the full day and accept that critical bugs may eat into the day — that's what it's for.
- **Pedro's sign-off criterion is subjective and load-bearing.** If Pedro doesn't want to use Mars tomorrow morning, Mars is not ready, period. Don't override this with "but the tests pass."
- **Dogfood notes are not a TODO list — they're data.** Capture raw impressions, friction points, delights, confusions. The v1.1 backlog emerges from analysis, not from transcription.
- **The PR reviewer daemon is not the product.** It's the *witness* to the product. Don't spend time polishing the system prompt beyond what's needed to prove the platform works.
- **If Pedro can't get through all 7 smoke tests in one day**, split this epic across Days 10 and 11. Day 11 normally belongs to the Maat template but the operator track can slip to Day 12 if the dev track isn't solid. Dev must ship before operator.
- **This epic has no new code to write except the example agent.yaml + prompt.** Everything else is bug fixes in earlier epics. Track the fixes against the affected epic, not here.
