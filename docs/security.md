# Mars Security Model — v1

**Status:** v1 shipping draft
**Audience:** Mars users (Pedro, Maat), future customers, auditors
**Related:** `docs/planning/epics/epic-09-security-and-launch.md`, `spikes/03-permission-roundtrip.md`, `apps/mars-runtime/src/session/permissions.py`

This document is an **honest, under-promising threat model** for Mars v1. It exists to tell you what Mars does and does not defend against so you can decide — with a clear head — whether to run it on sensitive data.

Mars v1 is a private-alpha product with one design partner and one operator-mode customer. It is not HIPAA, SOC 2, or ISO 27001. It has not been pen-tested. If any of those matter to you, wait for v2.

---

## TL;DR

- **You are the trust root.** Mars hosts code *you wrote* using API keys *you own*. The only thing Mars adds is the networked host + orchestration.
- **Secrets can be read by the daemon.** PreToolUse hooks are a *speed bump* against casual exfiltration, not a sandbox.
- **CLAUDE.md is admin-only.** The daemon literally cannot edit its own system prompt. Three layers of defense enforce this.
- **No multi-tenant isolation.** One Fly machine per Mars workspace. Your workspace is your VM, not a shared tenant on someone else's VM.
- **Auth is magic-link + JWT session cookies.** No passwords. No SMS 2FA. Tokens are single-use and 15-minute scoped.
- **Everything you send to the daemon is visible to Anthropic / OpenAI.** Mars does not proxy, rewrite, or redact your prompts before they hit the LLM provider.

---

## What Mars is (and is not)

Mars is:

- A runtime shell around the `claude` (Anthropic) and `codex` (OpenAI) CLIs. The CLIs do the heavy lifting; Mars runs them as subprocesses, parses their stream-json output, and exposes it via HTTP.
- A single-tenant control plane per workspace. Each workspace gets its own Fly.io app + machine + volume. No data crosses workspace boundaries.
- A "hosted terminal" for Claude Code / Codex — imagine `tmux` with a web UI and a Fly VM instead of your laptop.

Mars is not:

- A sandbox. The daemon runs arbitrary code the model decides to run, with whatever permissions the machine has. If you give it an `OPENAI_API_KEY`, it can print it.
- A secrets manager. Mars forwards secrets into the subprocess env. It does not rotate them, scope them by tool call, or redact them from logs.
- A zero-trust runtime. We assume the user wrote the `agent.yaml` in good faith and the LLM provider is not adversarial.
- A compliance product. There are no SOC 2 controls, no DPA, no audit trail suitable for regulated industries.

---

## Trust boundaries

```
┌─ You ──────────┐       ┌─ Mars ─────────┐       ┌─ Anthropic/OpenAI ─┐
│  agent.yaml    │──────▶│ Fly machine    │──────▶│  LLM + stream-json │
│  OAuth token   │       │ Supervisor     │       │  Tool calls        │
│  API keys      │       │ claude -p ...  │       │                    │
└────────────────┘       └────────────────┘       └────────────────────┘
       ▲                         │                         │
       └─── event stream ────────┘                         │
                                 │                         │
                                 └─── direct API calls ────┘
```

Mars sits between you and the LLM provider. Concretely:

1. **You → Mars**: `agent.yaml` + secrets + prompts. Over HTTPS with a magic-link-issued JWT session cookie. Mars trusts authenticated admins completely.
2. **Mars → LLM**: the `claude` / `codex` CLI calls the provider directly from the Fly machine. Mars does not proxy or inspect the HTTPS body. **Your prompts and the model's responses transit Anthropic / OpenAI exactly as if you ran `claude` on your laptop.**
3. **Mars → control plane**: the machine POSTs events outbound over HTTPS with an `X-Event-Secret` shared key. The control plane validates the secret and persists durable events to SQLite. Ephemeral events (text chunks) are fanned out via SSE but never stored.
4. **Control plane → browser**: SSE over HTTPS, JWT session cookie for auth, no cross-origin.

Everything inside one Fly machine — supervisor, `claude` subprocess, volume — shares a trust boundary. There is no sandboxing between them in v1.

---

## Threats Mars protects against

### 1. Prompt immutability (`CLAUDE.md` / `AGENTS.md` edits by the daemon)

The daemon must not be able to rewrite its own system prompt. Three layers:

1. **PreToolUse hook** in `apps/mars-runtime/claude_code_settings.json` that `exit 2`s when `Edit` / `Write` / `MultiEdit` targets any of `CLAUDE.md`, `AGENTS.md`, or `claude_code_settings.json`. See `apps/mars-runtime/hooks/deny-protected-edit.sh`. **This is the authoritative defense.**
2. **Filesystem read-only bind mount** (Epic 3 Dockerfile-level, belt-and-suspenders).
3. **Admin-only edit API** (`PATCH /agents/{name}/prompt` on the control plane → `POST /sessions/{id}/reload-prompt` on the supervisor). The admin UI and `mars edit-prompt` CLI use this path. The supervisor refuses any path that resolves outside `config.workdir` so admins can't smuggle a traversal payload either.

Verified by: `tests/runtime/test_claude_code_hooks.py` (32 subprocess tests exercising every protected filename), `tests/runtime/test_supervisor_api.py::test_reload_prompt_rejects_path_traversal` (traversal block), cross-layer consistency test in `tests/runtime/test_permissions.py` (baked settings.json matches `permissions.py` output).

### 2. Auth on the control plane

- **Magic-link email** via Resend. Tokens are short-lived JWTs signed with `MARS_MAGIC_LINK_SECRET`, audience-scoped to `mars-control:magic-link`, single-use (`jti` burned on first verify).
- **Session cookies** are *separate* JWTs signed with `MARS_SESSION_SECRET`, audience `mars-control:session`, 7-day TTL. A magic-link token cannot be reused as a session cookie (audience mismatch).
- **Cookies** are `HttpOnly`, `Secure`, `SameSite=Lax`. JS cannot read them; they do not travel over HTTP.
- **Protected routes** go through `make_current_user_dependency` which returns 401 with `WWW-Authenticate` on any unauthenticated request.

Verified by: `tests/control/test_auth_magic_link.py` (31 tests covering issue/verify, expiry, tampering, audience mismatch, single-use enforcement, end-to-end TestClient signin + logout).

### 3. Event forwarder auth (machine → control plane)

Every POST from the machine's `HttpEventForwarder` to the control plane's `/internal/events` ingest endpoint carries an `X-Event-Secret` header. The control plane compares in constant time via `hmac.compare_digest`. Missing or mismatched secret returns 401. Machines that can't produce the secret cannot poison the event store.

The shared secret is *per-machine*, set at `mars deploy` time via Fly secrets (injected into the container env). Rotating it requires redeploying the machine — v1 accepts this cost.

### 4. Secret-read bash speed bump

A second PreToolUse hook (`apps/mars-runtime/hooks/deny-secret-read-bash.sh`) blocks Bash commands at command position that match `env`, `printenv`, bare `set`, or `echo $...` patterns. This catches *accidental* exfiltration and *shallow prompt-injection attempts*. It does **not** block `python3 -c 'import os; print(os.environ[\"X\"])'` or a thousand other ways to read env vars.

### 5. Prompt-edit proposals captured, never applied

When the daemon's assistant text mentions `CLAUDE.md` or `AGENTS.md`, `memory/capture.py` writes it to `claude_md_proposals.jsonl` in the session memory dir for admin review. The file is write-only from the capture path. No code anywhere under `apps/mars-runtime/` applies a proposal back to the prompt file — `tests/runtime/test_memory_capture.py::test_proposals_are_captured_but_never_applied_to_prompt_file` asserts no file named `CLAUDE.md` is ever created inside the memory directory.

### 6. Supervisor control API is not publicly reachable

In production, the supervisor binds to the Fly machine's private network. The control plane reaches it over Fly's internal wireguard. Browsers never talk to the supervisor directly. An external attacker cannot hit `POST /sessions` on the supervisor without first getting onto your Fly org's network.

---

## Threats Mars does NOT protect against (explicit scope)

### 1. A malicious or compromised daemon reading env secrets

A daemon running `claude -p` has full access to the subprocess env. Any secret you forward via `agent.yaml`'s `env` list is reachable by any Python one-liner, curl body, or file read. The PreToolUse hook catches `echo $SECRET`; it does not catch `python3 -c 'import os; print(os.environ["SECRET"])'` or a hundred other exfiltration paths.

**Mitigation**: Only forward secrets to daemons you wrote and whose `agent.yaml` you audited. Treat every secret as potentially visible to Anthropic / OpenAI.

### 2. A compromised Mars control plane reading your data

If someone gets onto the Mars control plane's host, they can read:

- Every event that flowed through the ingest endpoint (SQLite `events.db`).
- Every session cookie ever issued (they can forge more with the shared secret).
- Every pending magic-link token (transient, 15-min).
- The control plane's Fly API token (stored in env).

**Mitigation**: Rotate `MARS_MAGIC_LINK_SECRET`, `MARS_SESSION_SECRET`, `MARS_EVENT_SECRET`, and `FLY_API_TOKEN` on every suspected compromise. All four are env-var configurable; no rebuild required.

### 3. Anthropic / OpenAI reading your prompts

The LLM provider sees every prompt and every tool result. Mars does not proxy, rewrite, or redact the HTTPS body. If your prompt contains PII, trade secrets, or regulated data, **that data leaves your trust boundary at the `claude -p` subprocess call**.

**Mitigation**: Assume every prompt is visible to Anthropic / OpenAI. Do not forward data you would not paste into Claude.ai.

### 4. Timing attacks on the magic link endpoint

The magic-link endpoint uses `hmac.compare_digest` for the token check, but does not yet enforce rate limits. An attacker who knows a victim's email can repeatedly request magic links and scan their inbox for them (or trigger mass email to annoy them).

**v1 accepts this.** Story 9.2 adds a 5-req/min rate limit per IP on `POST /auth/magic-link` which closes the worst case.

### 5. Side-channel attacks (timing, cache, Fly shared-tenancy)

Mars runs on Fly.io's shared infrastructure. Side-channel attacks across Fly VMs are out of scope — Fly's tenancy model is the authoritative defense, not ours.

### 6. Supply-chain attacks on `claude` / `codex` / `uv` / dependencies

Mars pins `@anthropic-ai/claude-code` to a specific version in the Dockerfile and pins Python deps in `pyproject.toml`. It does **not** verify package signatures, does **not** run SCA, and **trusts PyPI / npm**. A compromised upstream is a compromised Mars.

**Mitigation**: Bump pinned versions only after reviewing the upstream changelog. Keep an eye on public advisories.

### 7. Multi-tenant isolation

Mars v1 runs one Fly machine per workspace. There is no multi-tenant boundary within a machine: if your workspace has two sessions, they share the container but not the per-session `/workspace/<session-id>/` directory (Story 5.2 enforces per-session cwd). **They DO share the OS-level env vars.** A subtle bug in one session could read another session's subprocess args via `/proc` if the kernel exposes it.

**v1 accepts this**: Mars is single-workspace-per-VM and you are the admin. Multi-tenant v2 moves each session to its own Fly machine.

### 8. Out-of-band access via `mars ssh`

`mars ssh <agent>` opens a shell *as root* inside the machine. That shell can read every env var, every file, every stored credential. This is by design — `mars ssh` exists because `fly ssh console` exists. Anyone with `FLY_API_TOKEN` for your org can do the same thing directly.

**Mitigation**: Protect `FLY_API_TOKEN` like a production database password. It is the key to every Mars machine you've ever deployed.

---

## Env vars exposed to subprocesses

`apps/mars-runtime/src/session/claude_code.py::build_claude_env` uses an **explicit-allowlist** model. The subprocess env contains ONLY:

1. POSIX baseline: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ` (forwarded from parent if present).
2. Every name listed in `AgentConfig.env` (the `env:` list in your `agent.yaml`).
3. Any `extra_env` the supervisor passes (the Mars runtime itself forwards `CLAUDE_CODE_OAUTH_TOKEN` when baking Fly machines).

Explicitly NOT forwarded:
- `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_EXECPATH`, `CMUX_CLAUDE_PID` — these come from running under cmux on Pedro's dev machine; they are scrubbed **after** `extra_env` merge so a careless caller cannot reintroduce them.
- Any secret not in `config.env` — including `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, `FLY_API_TOKEN`, `MARS_MAGIC_LINK_SECRET`, `MARS_SESSION_SECRET`, `RESEND_API_KEY`. If the supervisor process has these on its env, they are still **not** forwarded to the daemon subprocess unless the `agent.yaml` explicitly names them.

`session/codex.py::build_codex_env` applies the same rules plus an additional allowlist: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` are always forwarded if present, because the codex CLI needs them to authenticate.

**HOME is a known trade-off.** Forwarding `HOME` exposes `~/.aws`, `~/.ssh`, `~/.config/gcloud` to the daemon *if those files exist on the container*. In production (Fly machine) `HOME` points at an empty volume; on local dev (`mars run --local`) it points at Pedro's actual home. Local dev is treated as a trusted boundary; production is treated as empty.

---

## Known limitations (v1.1 backlog)

These are real weaknesses we plan to address post-ship. They are not currently mitigated beyond "don't rely on them":

1. **No audit log of admin actions**. Who edited CLAUDE.md? Who killed a session? Not recorded.
2. **Session memory tarballs in S3 are SSE-S3 encrypted** (AWS-managed keys), not customer-managed. For higher-sensitivity deployments, v1.1 adds a KMS path.
3. **The control plane SQLite `events.db` is not encrypted at rest**. Fly volume encryption is the only protection.
4. **`Last-Event-ID` SSE resume is not implemented**. A browser reconnect after a gap sees a fresh stream from "now". The in-memory durable event history is available via a separate replay endpoint (TBD).
5. **No CSRF protection on POST endpoints**. The `SameSite=Lax` cookie attribute is the only defense. Real CSRF middleware lands in v1.1.
6. **No content-security-policy headers** on the frontend (there is no frontend yet — Epic 4 deferred).
7. **The magic-link email does not mention the requesting IP** or fingerprint the request. If a victim didn't request it, they cannot tell if the email is phishing vs. a real attempt by an attacker.

---

## Reporting a vulnerability

Email `pedro@camtomx.com` with the subject line `MARS SECURITY`. Do not file public GitHub issues for security bugs in v1.

We will confirm receipt within 48 hours. If you include a reproduction, we will credit you in the v1.1 shipping notes (with your permission).

---

## What changes at v2

v2 of Mars is the first release where this document gets *shorter*. Planned changes:

- Multi-tenant isolation via one Fly machine per session (not per workspace).
- Customer-managed encryption keys for session memory.
- Audit log of admin actions persisted in SQLite.
- `Last-Event-ID` SSE resume.
- CSRF tokens on state-changing endpoints.
- An outbound HTTP proxy that substitutes secret names for their values so the subprocess never sees the raw value (the v1 `secret-read bash speed bump` replaced with actual prevention).
- Anthropic + OpenAI API key rotation at the control-plane layer.

Until then: Mars v1 is what you see here. Run it on code you trust.
