# Epic 4 — Web UI & Magic-Link Auth

**Status:** `[x]` done (5/5 stories)
**Days:** 7 (one full day — ambitious but shaped)
**Depends on:** Epic 2 (needs SSE endpoint to subscribe to)
**Downstream blockers:** Epic 7 (dev dogfood needs a UI), Epic 8 (Maat turnkey extends this UI)
**Risk level:** MEDIUM (scope-wise tight)

## Summary

Build the Next.js web chat UI with the Vercel AI SDK `useChat` hook, render 4 component types (assistant text, tool call, tool result, permission request), show the session list dashboard, and add magic-link email signup via Resend with JWT session cookies. Everything the developer track needs to use Mars in a browser.

## Context

Pedro's v1 checklist includes "native chat" and "UI lists active sessions by name + description." This epic builds both. It also adds the public magic-link signup Pedro chose over manual provisioning, so anyone can sign up from day 1.

Magic-link auth is squeezed into the same day as the chat UI. If either side blows up the budget, the Maat template in Epic 8 is the buffer that absorbs slip.

## Scope

### In scope
**Frontend (Next.js app router)**
- `apps/mars-control/frontend/app/layout.tsx` — Root layout with auth guard
- `apps/mars-control/frontend/app/signup/page.tsx` — Email input form → triggers magic link
- `apps/mars-control/frontend/app/auth/verify/page.tsx` — Landing page for magic link click → sets session cookie
- `apps/mars-control/frontend/app/dashboard/page.tsx` — Session list with name + description + status
- `apps/mars-control/frontend/app/chat/[sessionId]/page.tsx` — Chat view for a specific session
- `apps/mars-control/frontend/components/chat/ChatView.tsx` — Main chat container (uses `useChat` from Vercel AI SDK)
- `apps/mars-control/frontend/components/chat/AssistantText.tsx` — Component #1: plain assistant message text
- `apps/mars-control/frontend/components/chat/ToolCall.tsx` — Component #2: tool invocation card (name + input)
- `apps/mars-control/frontend/components/chat/ToolResult.tsx` — Component #3: tool output (collapsible)
- `apps/mars-control/frontend/components/chat/PermissionRequest.tsx` — Component #4: approve/deny UI for pending tool calls
- `apps/mars-control/frontend/components/dashboard/SessionList.tsx` — List of active daemons
- `apps/mars-control/frontend/components/dashboard/SessionCard.tsx` — One daemon: name, description, status, "Open chat" button

**Backend (auth additions)**
- `apps/mars-control/backend/src/auth/magic_link.py` — Generate magic link token (JWT short-lived), store in DB (`magic_links` table), send via Resend
- `apps/mars-control/backend/src/auth/session.py` — Verify magic link → issue session JWT → set HTTP-only cookie
- `apps/mars-control/backend/src/auth/middleware.py` — FastAPI dependency that extracts + verifies the session JWT from cookie
- `apps/mars-control/backend/src/api/routes.py` — Routes: `POST /auth/magic-link`, `GET /auth/verify?token=...`, `POST /auth/logout`
- SQLite tables: `users(id, email, created_at)`, `magic_links(token_hash, user_id, expires_at, used)`

**Integration**
- Chat view subscribes to `GET /sessions/{id}/stream` SSE endpoint from Epic 2
- User message input POSTs to `POST /sessions/{id}/input` (proxied through control plane to machine supervisor)
- Permission request component wires approve/deny to `POST /sessions/{id}/permission-response`
- All backend routes protected by auth middleware except signup/verify

### Out of scope (deferred)
- Fancy chat rendering (markdown, syntax highlighting, file diffs) — v2
- Session creation UI — v1 uses `mars deploy` CLI; in-browser creation is v2
- Prompt editor UI (Epic 6 adds it for CLAUDE.md immutability flow)
- Template launcher (Epic 8 adds Templates tab)
- Teams / invites / RBAC (v2)
- Dark mode, theming (v2)
- Mobile responsive polish (v2 — v1 should be usable on mobile but not pretty)

## Acceptance criteria

- [ ] Fresh email → enter on `/signup` → receive magic link via Resend → click link → land on `/dashboard` with session cookie set
- [ ] Refresh `/dashboard` → still authenticated (cookie persists)
- [ ] Logout → `/dashboard` redirects to `/signup`
- [ ] `/dashboard` lists active sessions from `GET /sessions` (from control plane API) with name, description, status
- [ ] Click a session card → navigates to `/chat/[sessionId]`
- [ ] Chat view connects to SSE stream and renders events in real time
- [ ] Assistant text renders correctly (plain text, line breaks preserved)
- [ ] Tool call renders as a card with tool name + input (JSON pretty-printed)
- [ ] Tool result renders as a collapsible card with output
- [ ] Permission request renders an approve/deny button pair; clicking either sends the correct POST and the UI updates
- [ ] User typing a message + pressing enter → POSTs to `/sessions/{id}/input` → message appears in chat
- [ ] All protected routes 302 redirect to `/signup` if no valid session cookie
- [ ] Resend email configured with a test domain, magic link templates written in plain text + HTML
- [ ] Magic link tokens expire after 15 minutes and are single-use

## Critical files

| File | Purpose |
|---|---|
| `apps/mars-control/frontend/app/layout.tsx` | Root layout + auth guard |
| `apps/mars-control/frontend/app/signup/page.tsx` | Email input + magic link trigger |
| `apps/mars-control/frontend/app/dashboard/page.tsx` | Session list |
| `apps/mars-control/frontend/app/chat/[sessionId]/page.tsx` | Chat view |
| `apps/mars-control/frontend/components/chat/*.tsx` | 4 message type components |
| `apps/mars-control/backend/src/auth/magic_link.py` | Magic link generation + Resend integration |
| `apps/mars-control/backend/src/auth/session.py` | JWT session cookie management |
| `apps/mars-control/backend/src/auth/middleware.py` | FastAPI dependency for protected routes |

## Dependencies

- **Upstream:** Epic 2 (SSE stream endpoint to subscribe to)
- **Downstream:**
  - Epic 7 (dev dogfood needs a working UI to chat with the PR reviewer)
  - Epic 8 (Maat turnkey extends this UI with a Templates tab)

## Risks

| Risk | Mitigation |
|---|---|
| Vercel AI SDK `useChat` doesn't map cleanly to Mars's 4 event types | `useChat` is a starting point; if it fights the schema, drop it and use a raw SSE client (10 lines of `EventSource` JS). Don't spend >2 hours forcing it. |
| Resend setup (domain verification, DKIM) takes hours | Use Resend's sandbox/test mode for v1. Domain verification can wait until Epic 9. |
| SSE CORS issues between `control.mars.dev` and frontend | Configure CORS explicitly in FastAPI middleware. Same-origin recommended — serve frontend and backend from the same Fly/Vercel deploy if possible. |
| Magic link tokens leak via email forwarding | Single-use + 15 min expiry + HTTP-only cookie after verification. Document in security.md. |
| Next.js app router streaming gotchas | Use the `EventSource` browser API directly in a client component. Avoid Next.js's RSC streaming for v1 — it's under-documented for SSE consumption. |
| One-day budget unrealistic | Explicitly descope: no polish, no dark mode, no animations. UI should work and be ugly. Epic 9 buffer can absorb polish. |

## Definition of Done

- [ ] Code merged to `main`
- [ ] Deployed to a Vercel preview (or Fly instance) accessible via URL
- [ ] Signup → dashboard → chat flow works end-to-end with a real daemon
- [ ] All 4 chat component types render correctly against real event data
- [ ] Auth protection verified on every protected route
- [ ] Magic link email actually arrives (tested with Pedro's real inbox)

## Stories

Total: **5 stories**, ~8h budget. Tight for one day — if chat view or magic-link auth blows budget, descope polish first.

- [x] **Story 4.1 — Next.js scaffold + root layout + auth guard** (~1h)
  - *Goal:* Next.js app router scaffolding with `layout.tsx` that redirects unauthenticated users to `/signup`.
  - *Files:* `apps/mars-control/frontend/src/app/layout.tsx`, `apps/mars-control/frontend/src/app/page.tsx`, `apps/mars-control/frontend/src/lib/api.ts`, `apps/mars-control/frontend/src/lib/events.ts`, `apps/mars-control/frontend/.nvmrc`, `apps/mars-control/frontend/package.json`
  - *Done when:* unauthenticated visit to `/dashboard` redirects to `/signup`
  - *Outcome:* Scaffolded via `pnpm create next-app` with Next.js 16.2.3 + React 19 + Tailwind 4 + TypeScript + app router + `src/` layout + `@/*` alias. Node 22 pinned via `.nvmrc` (Next 16 min). Auto-generated `CLAUDE.md`, `AGENTS.md`, and `pnpm-workspace.yaml` were removed — they conflict with the runtime protection hooks and monorepo shape. **`src/lib/api.ts`** is the mars-control client: typed fetches for `requestMagicLink`, `verifyMagicLink`, `fetchCurrentUser`, `logout`, `listSessions`, `sendChatInput`, `updateAgentPrompt`, plus `sessionStreamUrl(id)` for the `EventSource` URL. All `fetch` calls pass `credentials: 'include'` so session cookies flow cross-origin in local dev. **`src/lib/events.ts`** is a hand-maintained TypeScript mirror of `apps/mars-runtime/src/events/types.py` with discriminator helpers (`isAssistantText`, `isToolCall`, `isToolResult`, `isPermissionRequest`, `isSessionStarted`, `isSessionEnded`). Docstring mandates that every Python event type change must land here in the same PR. **`src/app/layout.tsx`** is the Geist-fonts root with a sticky top header. **`src/app/page.tsx`** is the landing client component that calls `fetchCurrentUser` and redirects to `/dashboard` (authed) or `/signup` (unauthed). Build output: 7 static + 1 dynamic route, clean on Node 22.

- [x] **Story 4.2 — Magic-link backend (Resend + JWT cookie)** (~2h)
  - *Goal:* Backend endpoints to generate magic link tokens (15min, single-use), send via Resend, verify and issue JWT session cookie.
  - *Files:* `apps/mars-control/backend/src/mars_control/auth/__init__.py`, `apps/mars-control/backend/src/mars_control/auth/magic_link.py`, `apps/mars-control/backend/src/mars_control/auth/session.py`, `apps/mars-control/backend/src/mars_control/auth/middleware.py`, `apps/mars-control/backend/src/mars_control/auth/email.py`, `apps/mars-control/backend/src/mars_control/api/routes.py`, `tests/control/test_auth_magic_link.py`, `pyproject.toml` (+pyjwt, +email-validator)
  - *Done when:* email → Resend → click → JWT cookie set → authenticated request to protected route returns 200
  - *Outcome:* Four-module auth layer under `mars_control.auth`: (1) **`magic_link.py`** — `MagicLinkService` issues HS256 JWTs with 15-min default TTL, `jti` for single-use via an in-memory consumed-set, audience `mars-control:magic-link`, normalized lowercase emails; (2) **`session.py`** — `SessionCookieService` mints separate HS256 session JWTs with 7-day TTL, distinct audience (`mars-control:session`) so a magic-link token cannot be reused as a session cookie, `cookie_secure` configurable (True in prod, False for TestClient over http://); (3) **`middleware.py`** — `make_current_user_dependency` factory returning a FastAPI dep that reads the cookie + verifies + returns `SessionUser` or raises 401 with `WWW-Authenticate`; (4) **`email.py`** — `EmailSender` protocol, `ResendEmailSender` (POSTs to `api.resend.com/emails` with bearer auth), `InMemoryEmailSender` for tests. `create_control_app()` gained 4 optional kwargs to inject the services + pull production secrets from env vars (`MARS_MAGIC_LINK_SECRET`, `MARS_SESSION_SECRET`, `RESEND_API_KEY`, `MARS_FROM_EMAIL`, `MARS_MAGIC_LINK_BASE_URL`). Wired endpoints: `POST /auth/magic-link` (sends email, 202), `POST /auth/magic-link/verify` (consumes token, sets session cookie, 200), `POST /auth/logout` (clears cookie), `GET /me` (session-gated introspection), all returning 503 on environments that don't configure the auth stack. 31 unit tests: magic-link issue/verify/single-use/expiry/tampering/idempotent-consume; session issue/verify/audience-mismatch/expired/secure-flag; ResendEmailSender happy path + 4xx → `EmailSendError` + empty-arg validation; InMemoryEmailSender outbox recording; full end-to-end signin flow with TestClient (request magic link → extract token from outbox → verify → cookie set → `GET /me` returns user → `POST /auth/logout` clears); reuse rejection; garbage-token 401; protected route without cookie 401; no-auth-configured 503; invalid email 422. Full suite: 392 passed, 1 skipped.  **Stories 4.1/4.3/4.4/4.5 remain `[ ]` — they all need the Next.js frontend scaffold which has no home in mars-daemons yet.** 25/47 stories done.

- [x] **Story 4.3 — Signup + verify pages** (~1h)
  - *Goal:* Frontend signup form (email input) + verify landing page that receives token from URL and sets session cookie.
  - *Files:* `apps/mars-control/frontend/src/app/signup/page.tsx`, `apps/mars-control/frontend/src/app/auth/verify/page.tsx`
  - *Done when:* fresh email → submit → receive email → click link → land on `/dashboard` authenticated
  - *Outcome:* **`signup/page.tsx`** is a client form that calls `requestMagicLink(email)` and cycles through three UI states — idle/submitting, `sent` (with "check your inbox" message + the actual email echoed back + option to retry), and `error` (with the `MarsApiError` detail string). Submit button disables on empty/submitting. **`auth/verify/page.tsx`** pulls the `?token=` query param, calls `verifyMagicLink(token)`, and transitions through verifying / success (1-second pause showing "Welcome, email@…") / error. On success it `router.replace('/dashboard')` — 1-second pause is intentional so the browser gets a visible beat before the transition, feels like "logging you in" not "flashed and gone". `useSearchParams` required a `<Suspense fallback=…>` wrapper per Next.js 16's client boundary rules — the inner `VerifyInner` component reads the param; the outer page renders the Suspense shell.

- [x] **Story 4.4 — Dashboard + session list** (~1h)
  - *Goal:* `/dashboard` page that fetches `GET /sessions` and renders a card per session with name, description, status, and "Open chat" link.
  - *Files:* `apps/mars-control/frontend/src/app/dashboard/page.tsx`, `apps/mars-control/frontend/src/components/dashboard/SessionCard.tsx`
  - *Done when:* deployed daemons from `mars deploy` appear on `/dashboard` with correct name + description
  - *Outcome:* **`dashboard/page.tsx`** calls `fetchCurrentUser()` first (redirects to `/signup` on 401), then `listSessions()` in parallel. Shows three states: loading, error (with the detail string + retry button), and success. Success with `sessions.length === 0` shows an empty-state hint with a copy-pasteable `$ mars deploy examples/pr-reviewer-agent.yaml` command — this is the actual developer onboarding Pedro wanted (no in-browser creation in v1). Success with sessions renders a grid of `SessionCard` components. A header strip shows the logged-in email + a logout button that calls `logout()` then redirects. **`SessionCard.tsx`** renders session name (bold) + description (muted) + status badge (color-mapped: `running` → green, `exited_clean` → zinc, `exited_error`/`kill_timeout` → red, `killed` → amber) + a full-card link to `/chat/[sessionId]`. Gracefully shows "—" for missing description. Status label un-snake-cased for display (`exited_error` → `exited error`).

- [x] **Story 4.5 — Chat view + 4 message components + SSE client** (~3h)
  - *Goal:* `/chat/[sessionId]` page with `ChatView` that subscribes to SSE and renders `AssistantText`, `ToolCall`, `ToolResult`, `PermissionRequest` components, plus user input POSTing to supervisor.
  - *Files:* `apps/mars-control/frontend/src/app/chat/[sessionId]/page.tsx`, `apps/mars-control/frontend/src/components/chat/ChatView.tsx`, `apps/mars-control/frontend/src/components/chat/AssistantTextBubble.tsx`, `apps/mars-control/frontend/src/components/chat/ToolCallBubble.tsx`, `apps/mars-control/frontend/src/components/chat/ToolResultBubble.tsx`, `apps/mars-control/frontend/src/components/chat/PermissionRequestBubble.tsx`
  - *Done when:* live session streams events to browser in real time and user input round-trips through the daemon
  - *Outcome:* **`chat/[sessionId]/page.tsx`** uses Next.js 16's `use(params)` async-params pattern to unwrap `{sessionId}`, runs `fetchCurrentUser` auth-guard, renders the header with a "← Back to dashboard" link + the session id in mono, and mounts `<ChatView sessionId={sessionId} />`. **`ChatView.tsx`** is where the SSE lives — it creates an `EventSource(url, {withCredentials: true})` in a `useEffect`, then wires `addEventListener` for every known event name (`session_started`, `assistant_text`, `assistant_chunk`, `tool_call`, `tool_result`, `tool_started`, `permission_request`, `turn_completed`, `session_ended`, plus the default `message`) to the same `onMessage` handler that `JSON.parse`s `evt.data` into a typed `MarsEvent` and appends to the state array. The native `EventSource` API auto-reconnects on network blip (no `Last-Event-ID` replay in v1 — explicit note in the component's module docstring referencing Story 2.3's tradeoff). Connection status is tracked in `useState<'connecting'|'open'|'closed'|'error'>` and drives the input placeholder ("Connecting…"/"Reconnecting…"/"Type a message and press enter…"). Auto-scroll to bottom on new events via a `scrollRef.current.scrollTop = scrollRef.current.scrollHeight` effect. The bottom form POSTs via `sendChatInput(sessionId, input)` → control plane proxy → supervisor. Event routing uses the `isAssistantText` / `isToolCall` / `isToolResult` / `isPermissionRequest` / `isSessionStarted` / `isSessionEnded` discriminators from `lib/events.ts`. **4 bubble components**: `AssistantTextBubble` (black dot avatar + prose), `ToolCallBubble` (blue avatar + `tool_name` + pretty-printed JSON input), `ToolResultBubble` (zinc ← arrow + content, red border/bg when `is_error`, auto-truncates at 2000 chars with "… N more chars truncated" footer), `PermissionRequestBubble` (amber ! avatar + `permission denied · tool_name` + optional reason + collapsible "Attempted input" `<details>` showing the JSON — text is honest about v1 being advisory-only per `spikes/03-permission-roundtrip.md`). All bubble layouts share a 3-column flex (avatar / content / spacer) with `max-w-[80ch]` to keep lines readable.

## Notes

- **Vercel AI SDK `useChat` caveat:** it assumes OpenAI-compatible message format. Mars's event schema is different. The hook is useful for the typing/send/optimistic-update UI but may need a custom transform layer. Budget an hour for the integration; if it fights back, use raw `EventSource`.
- **Keep the UI ugly for v1.** Pedro has Malix design system at Camtom; Mars can adopt it LATER (v1.1). For now, Tailwind + default shadcn components. Polish is not a v1 requirement.
- **Magic link template** — use one of Resend's example templates with minor edits. Don't spend time on email design.
- **Session creation happens via CLI** in v1 (`mars deploy`). The dashboard is read-only + chat-only. Adding in-browser creation is a v2 feature that opens a new rabbit hole (file upload, validation, error states).
- **SSE reconnection:** the browser `EventSource` auto-reconnects on network blip. Good enough for v1. `Last-Event-ID` replay is v2.
- **One place to verify auth scope:** the backend middleware. Don't duplicate auth checks in the frontend — frontend just redirects when it sees 401 from an API call.
- **Resend alternative if you hit friction:** SMTP via Mailgun or Postmark. All three have Python SDKs. Pick Resend first because of cleaner DX, swap if needed.
