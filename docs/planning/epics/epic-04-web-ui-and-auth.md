# Epic 4 — Web UI & Magic-Link Auth

**Status:** `[ ]` not started
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

## Stories (to be decomposed next cycle)

*Placeholder — next session will break this into ~5 stories:*
- Story 4.1: Next.js scaffolding + root layout + auth guard
- Story 4.2: Magic-link backend (generate, send via Resend, verify)
- Story 4.3: Signup + verify pages (frontend)
- Story 4.4: Dashboard + session list page
- Story 4.5: Chat view with 4 message type components + SSE client

## Notes

- **Vercel AI SDK `useChat` caveat:** it assumes OpenAI-compatible message format. Mars's event schema is different. The hook is useful for the typing/send/optimistic-update UI but may need a custom transform layer. Budget an hour for the integration; if it fights back, use raw `EventSource`.
- **Keep the UI ugly for v1.** Pedro has Malix design system at Camtom; Mars can adopt it LATER (v1.1). For now, Tailwind + default shadcn components. Polish is not a v1 requirement.
- **Magic link template** — use one of Resend's example templates with minor edits. Don't spend time on email design.
- **Session creation happens via CLI** in v1 (`mars deploy`). The dashboard is read-only + chat-only. Adding in-browser creation is a v2 feature that opens a new rabbit hole (file upload, validation, error states).
- **SSE reconnection:** the browser `EventSource` auto-reconnects on network blip. Good enough for v1. `Last-Event-ID` replay is v2.
- **One place to verify auth scope:** the backend middleware. Don't duplicate auth checks in the frontend — frontend just redirects when it sees 401 from an API call.
- **Resend alternative if you hit friction:** SMTP via Mailgun or Postmark. All three have Python SDKs. Pick Resend first because of cleaner DX, swap if needed.
