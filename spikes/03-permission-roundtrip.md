# Spike 3 — Claude Code permission round-trip

**Date:** 2026-04-10
**Claude Code version:** 2.1.101 (pinned)
**Status:** Bidirectional primitive validated. v1 will ship with the
`acceptEdits` + allowlist + PreToolUse-hook fallback. Full schema of in-session
permission prompts is deferred to v1.1 investigation.

## The question

Can the Mars supervisor intercept Claude Code's permission prompts and respond
to them programmatically from the control plane, instead of relying on a
human at a TTY?

## What the CLI exposes (from `claude -p --help`)

**Permission modes** (`--permission-mode <mode>`):

| Mode | Behavior |
|---|---|
| `default` | Interactive — prompts before Edit/Write/Bash and other side-effect tools |
| `acceptEdits` | Auto-approves Edit/Write. Prompts for Bash and high-risk tools. |
| `bypassPermissions` | Fully automated. No prompts. Implied by `--dangerously-skip-permissions`. |
| `plan` | Read-only planning mode. Will not execute write tools. |
| `dontAsk` | Per CLI: accept without asking. Treated as approved-but-tracked. |
| `auto` | Per CLI: model-chosen behavior. Treat as opaque for v1. |

**Tool-level filters** (static, set at launch):

- `--allowed-tools <tools...>` / `--allowedTools` — allowlist. Supports scoped
  patterns like `Bash(git:*)` and plain tool names like `Edit`.
- `--disallowed-tools <tools...>` / `--disallowedTools` — denylist. Same
  syntax.
- `--tools <tools...>` — restrict the full built-in tool set.

**Bidirectional streaming primitive** (key for future round-trips):

- `--input-format stream-json` — reads line-delimited user events from
  stdin in real time.
- `--output-format stream-json` — emits events to stdout as they happen.
- `--replay-user-messages` — echoes user messages back on stdout for
  acknowledgment (only valid when both formats are `stream-json`).
- `--include-hook-events` — also emits `hook_started` / `hook_response`
  lifecycle events for PreToolUse / PostToolUse / etc.
- `--include-partial-messages` — streams partial assistant message deltas.

**Hooks** (`claude_code_settings.json`):

PreToolUse hooks run before every tool call, can block execution by exit
code, and can emit stdout that becomes part of the tool decision. These are
a **programmatic deny** surface — they can stop a tool, but they cannot
actively wait on an external human approval.

## What was validated in this session

### Host stream-json headless — works (spike 2)

Fully captured in `tests/contract/fixtures/stream_json_sample.jsonl`.
`result` event includes `permission_denials: []` — the CLI tracks denied
tools per session and surfaces them on exit.

### Bidirectional `stream-json` input — works

```bash
echo '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"say hi"}]}}' \
  | env -u CLAUDECODE -u CLAUDECODE_... \
    claude -p --input-format stream-json --output-format stream-json --verbose
```

Output sequence: `system.init → assistant(text="Hi") → rate_limit_event → result.success`.

This is the primitive Mars v1.1 will use for in-session user turn injection:
the supervisor holds `stdin` of the claude subprocess, receives new user
messages from the control plane via HTTP, and writes them as stream-json
lines. It is the same mechanism the web chat UI will drive.

### What is NOT validated

The exact wire schema of an in-session permission *prompt*: when
`--permission-mode default` is active and a tool call needs approval, what
event appears on stdout, and what shape of message does stdin expect to
approve/deny it? Running the interactive prompt under a real TTY produces
an inline UI — under `--print` / `--input-format stream-json` the CLI may
emit a structured "awaiting permission" event, or it may fall back to the
static allow/deny mechanisms. Verifying this would require either reading
Anthropic's SDK source or triggering a prompt under stream-json input and
observing the raw output.

## v1 decision — ship with `acceptEdits` + allowlist + PreToolUse denylist

For the 13-day v1 timeline, Mars adopts this three-layer setup in every
machine's supervisor and baked `claude_code_settings.json`:

1. **Session launch flags** (set by the supervisor when spawning each session):
   - `--permission-mode acceptEdits`
   - `--allowed-tools` — exact allowlist derived from the daemon's
     `agent.yaml` `tools[]` field (e.g. `Bash(git:*,pytest:*) Edit Read Grep`)
   - `--input-format stream-json --output-format stream-json` — bidirectional
     channel, even though v1 only uses it for user turn injection (not yet
     for permission responses)

2. **`claude_code_settings.json`** baked into `apps/mars-runtime/`:
   - PreToolUse hook blocking `Edit` / `Write` targeting `CLAUDE.md` and
     `AGENTS.md` (CLAUDE.md immutability — see v1 plan item 8)
   - PreToolUse hook blocking `Bash` commands matching
     `env|printenv|echo\s+\$` (secret-read speed bump — see v1 plan item 10)
   - Loaded via image bake, not via `--settings` (so the user agent.yaml
     cannot opt out)

3. **`docs/security.md` disclosure** (Epic 9): the v1 threat model is
   explicit — Mars runs code that Pedro or the user wrote, using keys the
   user owns, in a VM Mars operates. Tool approvals are advisory /
   allowlisted; true human-in-loop gates are a v1.1 feature.

### What v1 gives up (documented, acceptable)

- No mid-session "wait, approve this Bash command before it runs" dialog in
  the web UI. Tool calls either match the allowlist (run) or the denylist
  hook (blocked), full stop.
- `permission_denials[]` on the final `result` event is how the UI surfaces
  blocks after the fact.

### Failure pivot (if even `acceptEdits` turns out to misbehave under
stream-json input in Epic 3 testing)

- Downgrade to `--permission-mode bypassPermissions` plus a stricter
  allowlist + stricter PreToolUse denylist.
- Or use `--permission-mode plan` for read-only dogfood daemons (Pedro's
  PR reviewer can read and propose — it is acceptable for it not to write).

## v1.1 investigation list

Deferred to post-launch:

1. Trigger a default-mode permission prompt under `--input-format stream-json`
   and capture the raw stdout event that represents "awaiting approval".
2. Determine the exact stdin JSON shape that approves/denies a pending
   prompt (e.g. `{"type":"permission_response","tool_use_id":"...","decision":"allow"}`).
3. If no such schema exists in 2.1.x, track the Claude Code changelog for
   a future addition, or write a thin `expect`-style PTY wrapper as a
   last-resort Mars-side adapter.
4. Surface per-prompt approval in the web UI as a "Pending tool approval"
   banner that blocks the session until an admin clicks approve/deny.

None of these block v1 ship. All of them reuse the bidirectional primitive
proven in this spike.
