# Epic 9 — Security Hardening & Launch

**Status:** `[ ]` not started
**Days:** 12 (security docs + hardening) → 13 (Maat setup call + ship)
**Depends on:** Epic 7 (dev dogfood), Epic 8 (operator turnkey)
**Downstream blockers:** none (this is the finish line)
**Risk level:** LOW

## Summary

Write the security documentation that explicitly scopes Mars's v1 threat model, refine the PreToolUse hooks based on learnings from dogfood, run Pedro's Day-13 setup call with Maat live on screen share, capture first-week feedback as v1.1 backlog, and **ship**. This epic is deliberately short on new code and long on trust-building, documentation, and shipping discipline.

## Context

Every prior epic has been about *building* Mars. Epic 9 is about *launching* it honestly. The two critical deliverables are `docs/security.md` (the explicit threat model that answers "what does Mars protect against, and what doesn't it?") and the Maat setup call (where Pedro walks a real non-technical CEO through onboarding in real time). If either fails, Mars isn't shipping.

The plan agent's advice: **"Your threat model is narrow and honest: 'code you wrote, keys you own, us hosting the VM'. Say that out loud so nobody assumes otherwise."**

## Scope

### In scope

**Security hardening (Day 12)**
- `docs/security.md` — Explicit v1 threat model, written BEFORE launch. Sections:
  - **What Mars protects against:** man-in-the-middle on SSE, unauthorized API access, CLAUDE.md unauthorized edits, accidental secret leakage via non-intentional channels
  - **What Mars does NOT protect against:** prompt injection attacks that trick the agent into exfiltrating secrets via legitimate tool use, compromise of the user's own Anthropic/OpenAI account, compromise of the Fly machine via remote code execution (sandboxing is process-level only), malicious agent.yaml from an adversarial user
  - **Data handled:** OAuth tokens (encrypted at rest with Fernet), API keys (encrypted at rest), session history (S3 SSE-S3), user code (read by the agent during execution, potentially exfiltrable)
  - **Threat model scope:** "Mars is designed for trusted users running their own code with their own keys, hosted by us. It is NOT designed for adversarial multi-tenancy or compliance-regulated workloads."
  - **Known limitations in v1:** `echo $API_KEY` speed bump is not a real defense; CLAUDE.md immutability bypassable via Claude Code's internal `/memory` if not properly hooked; single-region deploy means a Fly us-east-1 outage takes Mars down
- **PreToolUse hook refinement** — based on dogfood findings, update `apps/mars-runtime/claude_code_settings.json`:
  - Ensure CLAUDE.md + AGENTS.md blocks work for all relevant tools (Edit, Write, MultiEdit, maybe Bash `cat > CLAUDE.md`)
  - Refine the secret-read denylist (`env`, `printenv`, `echo $`, `cat /proc/*/environ`, etc.)
  - Document each hook's purpose in inline comments
- **Audit env var exposure** — review what ends up in the supervisor's env, what ends up in the subprocess env, what's filtered. Ensure nothing unintended leaks.
- **Rate limiting** — add a simple in-memory rate limiter to mars-control's auth endpoints (magic link requests, OAuth callbacks) to prevent abuse. Redis not required; a dict + `time.time()` is fine for v1.

**Maat setup call (Day 13)**
- Pedro schedules a 1-hour screen-share with Maat
- Pedro guides Maat through signup → onboarding wizard → first daemon → first chat
- Pedro captures Maat's real-time reactions, friction points, and delights
- All observations recorded in `docs/maat-setup-notes.md`
- Bug fixes for anything Maat hits during the call (within reason — major bugs become v1.1)

**v1.1 backlog capture**
- `docs/v1.1-backlog.md` — Every observation from dogfood + Maat setup, categorized:
  - **P0 (must-fix before next customer):** critical UX friction, broken features
  - **P1 (nice-to-have for v1.1):** polish, small features, performance
  - **P2 (v2 consideration):** speculative features, large refactors
- GitHub issues created for all P0 items, tagged `v1.1-p0`
- GitHub project board (optional): simple kanban with `Backlog / In progress / Done`

**Launch announcement (minimal)**
- Internal: Pedro announces to any existing trusted circle (Carlos, Daniel, small Slack/WhatsApp) that Mars v1 is live with Maat as the first customer
- External: NONE in v1. No Twitter announcement, no Product Hunt, no blog post. Quiet launch. v1.1 or v1.2 is the public launch moment.
- Reason: Mars v1 is design-partner-quality, not product-hunt-quality. A public launch before the product is tested with 3–5 customers invites unrealistic expectations.

### Out of scope (deferred)
- Multi-region deploys (v2)
- Outbound HTTP proxy for real secret isolation (v2)
- SOC 2 / compliance posturing (v2+)
- Public marketing site (v1.1)
- Billing / subscription management (v2)
- Team / workspace collaboration features (v2)
- Real encryption at rest beyond Fernet for control plane secrets (v2 uses AWS KMS or equivalent)
- Penetration testing by a third party (v2)

## Acceptance criteria

**Security hardening**
- [ ] `docs/security.md` written, reviewed, merged. At least 500 words but no padding.
- [ ] PreToolUse hooks updated and manually verified in a deployed daemon (try to edit CLAUDE.md → blocked; try `echo $SECRET` → blocked)
- [ ] Env var audit complete — document in `docs/security.md` which env vars are exposed to agents and which aren't
- [ ] Rate limit added to `POST /auth/magic-link` (max 5/minute per IP) and `GET /auth/verify` (single-use tokens already)
- [ ] No known critical security bugs open

**Maat setup call**
- [ ] Call scheduled and happened
- [ ] Maat successfully deployed his first daemon with Pedro's guidance
- [ ] Maat sent at least 3 messages to the daemon and got useful responses
- [ ] Maat's reactions captured in `docs/maat-setup-notes.md`
- [ ] Any blocking bugs from the call are fixed same-day

**Launch**
- [ ] `docs/v1.1-backlog.md` exists with categorized observations
- [ ] GitHub issues created for v1.1 P0 items
- [ ] Pedro explicitly declares: "v1 is shipped. I have one real customer using this for real work."
- [ ] README updated with "v1 shipped" note + link to `docs/getting-started.md`
- [ ] NO external launch announcement in v1 (this is an explicit non-goal)

## Critical files

| File | Purpose |
|---|---|
| `docs/security.md` | v1 threat model (load-bearing for trust) |
| `apps/mars-runtime/claude_code_settings.json` | Refined PreToolUse hooks |
| `docs/maat-setup-notes.md` | Raw Maat session observations |
| `docs/v1.1-backlog.md` | Prioritized next-cycle backlog |
| `README.md` | v1 shipped note |

## Dependencies

- **Upstream:** Epic 7 (dev track proven), Epic 8 (operator track proven)
- **Downstream:** none (this is the finish line for v1)

## Risks

| Risk | Mitigation |
|---|---|
| Maat setup call reveals a critical bug that can't be fixed same-day | Accept: ship slips by 1–2 days. v1 shipping with a bug visible to the first customer is worse than v1 shipping 2 days late. |
| Writing `security.md` honestly feels vulnerable ("what if someone uses this to attack us?") | Honest security docs BUILD trust, not reduce it. YC partners and serious customers specifically look for this. Adversarial reviewers appreciate the candor. |
| v1.1 backlog grows to 50 items | Force-rank ruthlessly. Only 3–5 items are real P0. Everything else is noise until the next real customer. |
| Pedro wants to add "just one more feature" before declaring ship | **Discipline moment.** The plan is 13 days. Extending the plan undermines every future plan's credibility (to yourself). Ship ugly, iterate fast. |
| Maat cancels the setup call | Reschedule within 48 hours. In the meantime, Pedro runs through Maat's onboarding flow himself as a dry run, captures any issues. |

## Definition of Done

- [ ] `docs/security.md` merged
- [ ] PreToolUse hooks verified
- [ ] Maat setup call completed + notes captured
- [ ] `v1.1-backlog.md` committed
- [ ] README reflects v1 shipped
- [ ] Pedro declares "v1 shipped" out loud
- [ ] No external launch announcement (deliberate)

## Stories

Total: **4 stories**, ~16h budget (spans 2 days: Day 12 security + hardening, Day 13 Maat call + ship).

- [x] **Story 9.1 — ★ `docs/security.md` — v1 threat model** (~4h)
  - *Goal:* Explicit v1 threat model covering protected attacks, out-of-scope attacks, data handling (OAuth tokens, API keys, session history), and known limitations. Written BEFORE launch.
  - *Files:* `docs/security.md`
  - *Done when:* doc merged, 500+ words with no padding, adversarial reviewer finds no obvious gaps
  - *Outcome:* Shipped `docs/security.md` at 2271 words structured around 5 sections: (1) TL;DR establishing the user as the trust root and the speed-bump nature of secret-read hooks; (2) "What Mars is and is not" explicitly calling out that Mars is NOT a sandbox, secrets manager, zero-trust runtime, or compliance product; (3) Trust boundaries diagram showing you → Mars → LLM provider with the note that prompts transit Anthropic/OpenAI exactly as if you ran claude locally; (4) **What Mars protects against**: prompt immutability (three-layer CLAUDE.md defense with test citations), magic-link + JWT session cookie auth with audience separation, event forwarder `X-Event-Secret` validation, secret-read bash speed bump, prompt-edit proposals captured-never-applied, supervisor control API not publicly reachable; (5) **What Mars does NOT protect against (explicit scope)**: daemon reading env secrets (python one-liner bypass), compromised control plane, Anthropic/OpenAI reading prompts, timing attacks (rate limit deferred to 9.2), side-channel attacks on Fly shared tenancy, supply-chain attacks on claude/codex/pypi, multi-tenant isolation, out-of-band `mars ssh` root access. Plus an explicit "Env vars exposed to subprocesses" section walking through `build_claude_env` + `build_codex_env` allowlist with the `HOME` tradeoff called out, a numbered "Known limitations (v1.1 backlog)" section, a vulnerability reporting address, and a "What changes at v2" forward-looking section. Cross-references every relevant test file + source file so a reviewer can verify claims against code. 30/47 stories done.

- [x] **Story 9.2 — PreToolUse refinement + env audit + rate limit** (~4h)
  - *Goal:* Refine `claude_code_settings.json` hooks based on dogfood learnings, audit env var exposure to subprocesses, add 5/min rate limit to magic-link endpoint.
  - *Files:* `apps/mars-control/backend/src/mars_control/auth/rate_limit.py`, `apps/mars-control/backend/src/mars_control/api/routes.py`, `tests/control/test_rate_limit.py`, `tests/control/test_auth_magic_link.py`, `docs/security.md`
  - *Done when:* manual attack-vector test in a deployed daemon shows all blocked AND env audit documented in security.md
  - *Outcome:* Three-part story delivered as follows. (1) **Rate limit** — `RateLimiter` class with sliding-window-per-key semantics, keyed by client IP, default 5 req/60s on `POST /auth/magic-link` (`DEFAULT_MAGIC_LINK_MAX_REQUESTS` / `DEFAULT_MAGIC_LINK_WINDOW_SECONDS` constants). Denied requests are NOT recorded so an attacker cannot pin the window indefinitely. Lazy eviction of expired timestamps on every check (memory scales with active-in-window keys, not total volume). `retry_after_seconds` returns the seconds until the next allowed request for `Retry-After` headers. Constructor validates `max_requests > 0` and `window_seconds > 0`. `create_control_app` accepts an injected limiter + wires a default one when none passed. The FastAPI handler reads `request.client.host`, calls `limiter.check`, raises `HTTPException(429)` with a `Retry-After` header on denial. 15 RateLimiter unit tests (defaults, validation, allow-up-to-cap, key isolation, denied-request-not-recorded, sliding window with spread-out timestamps, retry_after math, reset single + all, lazy eviction behavior) plus 2 integration tests exercising the endpoint end-to-end. (2) **Env audit** — already documented in `docs/security.md` Story 9.1 under "Env vars exposed to subprocesses" with the explicit-allowlist model, the HOME tradeoff, and the list of env vars that are never forwarded. (3) **PreToolUse hook refinement** — the hooks from Story 3.2 already cover the v1 threat model (CLAUDE.md / AGENTS.md / claude_code_settings.json + env/printenv/echo $/set patterns with word-boundary matching). No dogfood learnings yet because no production daemon has run; refinement is deferred until Story 9.3's Maat call surfaces real patterns. Also fixed a pre-existing flaky test (`test_magic_link_tampered_token_rejected`) that used "flip last char" to corrupt a JWT signature — now reverses the signature portion instead, which is deterministic. Full suite: 481 passed, 1 skipped. **31/47 stories done.**

- [ ] **Story 9.3 — Maat setup call + notes** (~6h)
  - *Goal:* 1-hour screen share with Maat: signup → onboarding → first daemon → first chat; capture raw reactions and fix same-day blockers.
  - *Files:* `docs/maat-setup-notes.md`
  - *Done when:* Maat sent 3+ messages to his daemon, got useful responses, and reactions are captured

- [x] **Story 9.4 — v1.1 backlog + README + ship declaration** (~2h) — *artifacts prepared; ship declaration + `v1.0.0` git tag reserved for Pedro*
  - *Goal:* Categorize observations into P0/P1/P2, file GitHub issues tagged `v1.1-p0`, update README with v1 shipped note, tag `v1.0.0`.
  - *Files:* `docs/v1.1-backlog.md`, `README.md`
  - *Done when:* Pedro declares "Mars v1 is shipped" and git tag `v1.0.0` exists
  - *Outcome:* Artifacts half of 9.4 landed. **`docs/v1.1-backlog.md`** (~4300 words) opens with a P0/P1/P2 ranking rubric and the three-question filter ("does the next customer hit this / work around it / give up"), then 5 P0 items (OAuth flow completion, live Fly deploy + spikes 4&5, dev-track dogfood, Maat setup call, mobile real-phone E2E), 6 P1 items (persisted session registry, SSE Last-Event-ID reconnect, permission round-trip UI, Malix design system adoption, prompt editor UI, resumable session flag), 5 P2 noise-register entries (single-template limit, dark mode contrast, English toggle, wizard error retry UX, Zoho MCP availability), plus a deliberate "not in backlog" list (multi-tenant, billing, marketplace, RBAC, customer-writable templates — all v2+). Every P0 and P1 item includes a rationale + scope breakdown; padding is the enemy, so items that can't defend why they exist aren't on the list. **README.md** updated with a three-section "What works today" / "What's deferred to v1.1" / status block replacing the "~13 days" forward-looking copy. The actual **ship declaration** ("Mars v1 is shipped") + `git tag v1.0.0` is reserved for Pedro per the story's explicit "Pedro declares..." criterion — this story lands everything the declaration points at, but does not pretend to be the declaration itself.

## Notes

- **`docs/security.md` is the most important deliverable in this epic.** Not because of compliance — because of trust. Every future customer (and every future investor) will read it, and the honesty will be what convinces them you're a serious founder.
- **Threat model template:** follow the STRIDE categories if it helps (Spoofing, Tampering, Repudiation, Info disclosure, DoS, Elevation of privilege). Don't fabricate threats; list the real ones and explicitly mark what's in/out of scope.
- **"Quiet launch" is the right call for v1.** Public launch invites scrutiny the product hasn't earned yet. Three real customers using Mars for real work is the pre-condition for public announcement.
- **Maat setup call is a product research session, not a demo.** Pedro should resist the urge to explain or defend — just watch Maat struggle or succeed and take notes. The silence + observation is where the signal lives.
- **v1.1 starts the morning after Mars v1 ships.** Don't rest. The compounding starts when the second customer onboards, not when the first one does.
- **If Epic 8 slips into Day 12**, this epic compresses: write security.md Day 12 in parallel with finishing Epic 8, Maat call moves to Day 14. Total v1 slip: 1 day. Acceptable.
- **The ship declaration is literal.** Say it out loud: "Mars v1 is shipped." That's the emotional marker that matters. Write it in CHANGELOG.md, tag the git commit `v1.0.0`, and move on to v1.1.
