# Epic 8 — Operator Turnkey (Maat)

**Status:** `[ ]` not started
**Days:** 11 (one full day)
**Depends on:** Epic 3 (Fly deploy), Epic 4 (Web UI + auth), Epic 7 (dev track proven)
**Downstream blockers:** Epic 9 (launch requires Maat track complete)
**Risk level:** **HIGH (product risk)** — without this, Maat cannot use Mars and the design-partner thesis collapses

## Summary

Build the operator-facing surface of Mars so that Maat (a non-technical SMB CEO who doesn't know what Claude Code is) can go from signup to a working AI agent in a chat window on his phone in under 10 minutes. Zero YAML editing. Zero CLI commands. One pre-baked template (`tracker-ops-assistant`) + an onboarding wizard + a Templates tab in the dashboard.

## Context

The plan agent's most important product insight: **Pedro's 10-item v1 checklist is developer-shaped, but Maat cannot use a YAML-and-CLI product.** Without an operator layer, Maat is a vanity metric, not a customer. This epic exists to make Mars a real product for real non-technical users — Pedro's design partner thesis depends on it.

The trick is that the substrate is already built: Epics 1–6 cover everything Mars needs to run a daemon. This epic is essentially a *UX layer* that hides the mars-daemons primitives behind a "hit Start" button.

## Scope

### In scope

**Pre-baked template**
- `apps/mars-control/templates/tracker-ops-assistant.yaml` — complete agent.yaml for an ops assistant daemon:
  - Runtime: `claude-code`
  - MCPs: WhatsApp MCP, Zoho MCP (or placeholder), Pilot browser MCP
  - System prompt (`tracker-ops-assistant.prompt.md`): "You are the operations assistant for Orion, a tracker company. You have access to WhatsApp for communication, Zoho for CRM, and the company's internal systems. Your job is to..."
  - Secrets references: `${secret:zoho_api_key}`, `${secret:whatsapp_session}`, etc.
  - Placeholders that the onboarding wizard fills in

**Template discovery + launcher**
- `apps/mars-control/frontend/app/dashboard/page.tsx` — Add a "Templates" tab next to "Sessions"
- `apps/mars-control/frontend/components/templates/TemplateList.tsx` — Card grid of available templates (just 1 for v1)
- `apps/mars-control/frontend/components/templates/TemplateCard.tsx` — Template card with name, description, "Start" button
- `apps/mars-control/backend/src/api/routes.py` — `GET /templates` returns available templates from the `apps/mars-control/templates/` directory

**Onboarding wizard (the key UX)**
- `apps/mars-control/frontend/components/templates/OnboardingWizard.tsx` — Multi-step modal:
  - **Step 1:** "Welcome to Tracker Ops Assistant. This AI agent will help you manage your operations." + "Start"
  - **Step 2:** "You need a Claude account to power this agent. We'll walk you through it." → button opens Claude.ai signup in new tab → "I have an account" button
  - **Step 3:** "Subscribe to Claude Max ($200/month) to get enough capacity. Cancel anytime." → button opens Claude Max subscription → "Subscribed" button
  - **Step 4:** "Now connect your Claude account to Mars." → Anthropic OAuth flow → returns with token → stored encrypted in control plane
  - **Step 5:** "Connect your tools." → form with Zoho API key, WhatsApp MCP config, other required secrets. Labels in Spanish AND English.
  - **Step 6:** "Deploy!" button → spinner showing deploy progress (creating machine, injecting secrets, spawning session) → success → "Open chat" button
- `apps/mars-control/backend/src/oauth/anthropic.py` — Anthropic OAuth flow implementation (initiate, callback, store encrypted token)
- `apps/mars-control/backend/src/api/routes.py` — `POST /templates/{name}/deploy` endpoint that takes secrets + creates workspace + deploys daemon using the template

**Mobile-friendly chat**
- Verify `apps/mars-control/frontend/app/chat/[sessionId]/page.tsx` works on mobile (phone viewport)
- Minimal responsive tweaks only (message bubbles fit screen, input doesn't get hidden behind keyboard)
- **Not** full mobile optimization — just "usable on a phone"

### Out of scope (deferred)
- More than one template (v1.1 adds customs ops, sales qualification, etc.)
- Template editing by users (v2 — v1 templates are read-only)
- Billing (v1 is free for design partners; v2 adds Stripe)
- Admin dashboard for managing templates (v2)
- In-app Claude Max subscription purchase (v1 redirects out to Anthropic)
- Marketplace / community templates (v2)

## Acceptance criteria

- [ ] `tracker-ops-assistant.yaml` is a valid `AgentConfig` that passes `AgentConfig.parse_file()`
- [ ] System prompt is in Spanish (Maat's language) but includes English keywords for Claude
- [ ] Dashboard has a "Templates" tab visible after signup
- [ ] "Tracker Ops Assistant" card shows name, description, "Start" button
- [ ] Clicking "Start" opens the onboarding wizard
- [ ] Wizard steps 1–6 flow cleanly without errors
- [ ] Anthropic OAuth flow: click "Connect Claude" → redirect to Claude.ai → authorize → return to wizard with token captured
- [ ] Secrets form accepts Zoho API key, stores encrypted in control plane
- [ ] Final deploy button creates a Fly machine, injects secrets, spawns session, returns chat URL
- [ ] Chat URL opens on Maat's phone browser and works (message send, event stream, responses)
- [ ] **The entire flow from signup → chat takes <10 minutes for a first-time user who doesn't know what Claude Code is**
- [ ] Maat explicitly never sees: `agent.yaml`, `mars` CLI, terminal, Dockerfile, any YAML, any Python, any SSH, any Fly dashboard
- [ ] Error states handled gracefully: "OAuth failed", "secret invalid", "deploy failed" — all show human-readable messages with a retry option

## Critical files

| File | Purpose |
|---|---|
| `apps/mars-control/templates/tracker-ops-assistant.yaml` | Pre-baked template |
| `apps/mars-control/templates/tracker-ops-assistant.prompt.md` | System prompt in Spanish |
| `apps/mars-control/frontend/components/templates/TemplateList.tsx` | Template grid |
| `apps/mars-control/frontend/components/templates/OnboardingWizard.tsx` | Multi-step modal |
| `apps/mars-control/backend/src/oauth/anthropic.py` | Anthropic OAuth flow |
| `apps/mars-control/backend/src/api/routes.py` | `GET /templates`, `POST /templates/{name}/deploy` |

## Dependencies

- **Upstream:**
  - Epic 3 (`mars deploy` under the hood)
  - Epic 4 (dashboard + auth + chat UI)
  - Epic 7 (dev track proven working, Pedro's daily use validates the substrate)
- **Downstream:** Epic 9 (Maat setup call)

## Risks

| Risk | Mitigation |
|---|---|
| Anthropic OAuth flow requires a registered OAuth client (client_id/secret) that Anthropic may not issue quickly | **Research before Day 11.** If OAuth client takes days to issue, fall back to "paste your `CLAUDE_CODE_OAUTH_TOKEN`" in a text field. Less elegant but ships. |
| Maat doesn't have a credit card ready for Claude Max during the call | Day 13 (Maat setup call) is scheduled specifically so Pedro can walk Maat through this live. Not a v1 gate. |
| Wizard UX is buggy on Maat's specific phone/browser combo | Test on real iOS Safari + Android Chrome before the setup call. |
| WhatsApp MCP setup is complex (requires pairing, device linking, etc.) | Acknowledge in v1 scope: Maat will need help from Pedro for the WhatsApp step during the setup call. Document as a known v1 friction point. |
| Zoho API key requires Zoho developer account which Maat doesn't have | Pre-arrange with Maat: he provides an existing API key during the setup call. Or if Zoho key unavailable, v1 template works without Zoho as long as WhatsApp works. |
| Cold boot >30s causes the wizard's final "Deploy!" step to feel broken | Add a progress indicator with stages: "Creating VM → Injecting secrets → Starting daemon → Connecting..." so the user sees activity. If Spike 5 showed >30s, this indicator is the UX save. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] Onboarding wizard flows end-to-end on a mobile browser
- [ ] Tested on Pedro's phone with a fresh Mars account + fresh Anthropic account
- [ ] Spanish copy reviewed (Pedro is native speaker, verify it doesn't sound weird)
- [ ] At least one successful Template deploy → chat → message → response loop completed
- [ ] Ready to hand to Maat on Day 13

## Stories

Total: **5 stories**, ~8h budget. Without this epic, Maat cannot use Mars and the design-partner thesis collapses.

- [x] **Story 8.1 — `tracker-ops-assistant.yaml` + Spanish system prompt** (~1h)
  - *Goal:* Pre-baked template agent.yaml with WhatsApp/Zoho/Pilot MCPs + Spanish system prompt tuned for Orion ops assistant, with placeholder secrets the wizard fills in.
  - *Files:* `apps/mars-control/templates/tracker-ops-assistant.yaml`, `apps/mars-control/templates/tracker-ops-assistant.prompt.md`, `tests/control/test_template_tracker_ops.py`
  - *Done when:* `AgentConfig.parse_file()` succeeds on the template
  - *Outcome:* Landed both files. **YAML** declares `runtime: claude-code`, workdir `/workspace/tracker-ops-assistant`, MCPs `[whatsapp, zoho, pilot]`, tools `[Read, Write, Edit, Bash, Grep, Glob]`, env names for the 7 secrets the wizard will collect (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `WHATSAPP_SESSION_NAME`, 3x `ZOHO_*`, `PILOT_SESSION_NAME`). Validated against `AgentConfig.from_yaml_file` — parses clean. **Prompt** is Spanish-first (mexicano, no English), structured as: persona + style (speak clearly, no jargon, answer-then-ask, never hallucinate), tools catalog (whatsapp/zoho/pilot/filesystem — each with rules), explicit "what you do NOT do" list (no billing, no strategic decisions, no promises without system confirmation), and a daily routine (read inbox → triage → write daily summary). The prompt bakes in the Orion-specific framing (customs broker-style rules, daily summary filename convention, owner-vs-assistant scope boundary). 7 unit tests: file existence, YAML validity, MCPs set, env secrets set, Spanish stop-word detection (`Eres`, `español`, `Tu trabajo`, `herramientas`), prompt mentions all 3 MCPs, description contains `español`.

- [ ] **Story 8.2 — Templates tab + discovery API** (~1h)
  - *Goal:* Dashboard Templates tab with card grid rendering templates from `GET /templates`.
  - *Files:* `apps/mars-control/frontend/app/dashboard/page.tsx`, `apps/mars-control/frontend/components/templates/TemplateList.tsx`, `apps/mars-control/frontend/components/templates/TemplateCard.tsx`, `apps/mars-control/backend/src/api/routes.py`
  - *Done when:* Templates tab shows the Tracker Ops Assistant card with a working Start button

- [ ] **Story 8.3 — OnboardingWizard steps 1–3** (~2h)
  - *Goal:* Multi-step modal with welcome screen + Claude.ai signup redirect + Claude Max subscription redirect, with Spanish copy throughout.
  - *Files:* `apps/mars-control/frontend/components/templates/OnboardingWizard.tsx`
  - *Done when:* steps 1–3 navigate forward/back cleanly on mobile viewport

- [ ] **Story 8.4 — ★ OnboardingWizard steps 4–6 + OAuth + deploy endpoint** (~3h)
  - *Goal:* Anthropic OAuth flow (initiate/callback/encrypted store) + secrets form + `POST /templates/{name}/deploy` endpoint creating workspace + deploying daemon with SSE stage progress updates.
  - *Files:* `apps/mars-control/backend/src/oauth/anthropic.py`, `apps/mars-control/backend/src/api/routes.py`, `apps/mars-control/frontend/components/templates/OnboardingWizard.tsx`
  - *Done when:* fresh account → OAuth → secrets → deploy → chat opens, total elapsed <10min on mobile

- [ ] **Story 8.5 — Mobile responsive sanity + real-phone E2E** (~1h)
  - *Goal:* Chat view + wizard verified on real iOS Safari + Android Chrome with no broken input or keyboard overlap.
  - *Files:* `apps/mars-control/frontend/app/chat/[sessionId]/page.tsx`
  - *Done when:* end-to-end template deploy → chat message → response tested on Pedro's phone

## Notes

- **The wizard copy matters more than the code.** Spend time on the words — that's what Maat will read. Test copy with a non-technical friend before the setup call.
- **Spanish first, English second.** The wizard should be in Spanish by default for the Maat launch. Add English toggle later.
- **"Start Tracker Ops Assistant"** — not "Deploy daemon to Mars" or "Launch instance". Operator language, not developer language.
- **Anthropic OAuth research is Day 10 prep work.** Don't wait until Day 11 to discover OAuth clients take 3 days to approve. Research client_id registration during Epic 7 bug fix time.
- **Claude Max subscription is OUT of Mars's control.** Maat subscribes directly with Anthropic. Mars just verifies he has an active subscription by making a test API call with the OAuth token.
- **The Zoho MCP** may not exist yet as a public MCP. If not, v1 uses a placeholder or a simple HTTP adapter Pedro writes during this epic. Document the gap.
- **Mobile testing** — don't emulate. Use an actual phone. iOS Safari and Android Chrome have different quirks; test at least one of each.
- **Progress indicators during deploy** are cheap to add and massively improve perceived speed. The `POST /templates/{name}/deploy` endpoint should stream SSE with stage updates: `{stage: "creating_vm"}`, `{stage: "injecting_secrets"}`, etc.
- **If the wizard is taking >1 day to build**, the scope is wrong — cut to: skip step 2 (assume user has Claude account), skip the final "Open chat" button (just redirect), skip the multi-step animation. Ship ugly.
