# Mars Daemons v1 — Epics Index

**Status:** Ready for story decomposition (next cycle)
**Plan reference:** `/Users/pedrorios/Desktop/mars-daemons-v1-plan.md`
**Repo:** `github.com/tacosyhorchata/mars-daemons` (not created yet)
**Total:** 10 epics spanning 13 days
**Created:** 2026-04-10

## Progress legend
- `[ ]` not started · `[~]` in progress · `[X]` done · `[!]` blocked

## How to use

1. **This cycle:** read the index + each epic file to understand scope, dependencies, and acceptance criteria.
2. **Next cycle:** decompose each epic into stories (2-6 stories per epic, each shippable in half a day or less). Stories go in `epic-XX-name.md` under the `## Stories` section.
3. **Execution cycles:** pick one story at a time, implement, mark done, move to next.

## Epic dependency graph

```
                    Epic 0 — Foundation & Spikes
                              │
                              ▼
                   Epic 1 — Supervisor & Parser ★ HIGH RISK
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
   Epic 2 — Event       Epic 6 — Local,       (also feeds
   Forwarding & SSE     Immutability,         Epic 3 below)
             │          Memory
             ▼                │
   Epic 3 — Fly Deploy        │
   & Docker                   │
             │                │
             ├────────────────┤
             ▼                ▼
   Epic 5 — Multi-Sess   Epic 4 — Web UI
   & Recovery            & Magic-Link Auth
             │                │
             └────────┬───────┘
                      ▼
            Epic 7 — Dev Dogfood (Pedro)
                      │
                      ▼
            Epic 8 — Operator Turnkey (Maat)
                      │
                      ▼
            Epic 9 — Security & Launch
```

★ = highest-risk epic (stream-json parser)

## Epics table

| # | Epic | Days | Risk | Depends on | Summary |
|---|---|---|---|---|---|
| 0 | [ ] [Foundation & Spikes](epic-00-foundation-and-spikes.md) | 1–2 | HIGH | — | Validate 3 hard unknowns (CC OAuth, stream-json, permissions) in parallel with repo scaffold + `agent.yaml` schema. Gates everything. |
| 1 | [ ] [Supervisor & stream-json Parser](epic-01-supervisor-and-parser.md) | 2–3 | **CRITICAL** | 0 | `mars-runtime` supervisor skeleton + the JSONL parser that translates Claude Code output into Mars events. Highest-risk file in project. |
| 2 | [ ] [Event Forwarding & SSE Topology](epic-02-event-forwarding-and-sse.md) | 4 | MEDIUM | 1 | Machine POSTs events outbound to control plane via `HttpEventSink`. Control plane holds browser SSE fanout. Single SSE hop. |
| 3 | [ ] [Fly Deploy & Dockerization](epic-03-fly-deploy-and-docker.md) | 5–6 | MEDIUM | 1, 2 | `mars-runtime` Dockerfile, `claude_code_settings.json` security hooks, `mars deploy` CLI, `mars ssh` wrapper, Fly.io REST client. |
| 4 | [ ] [Web UI & Magic-Link Auth](epic-04-web-ui-and-auth.md) | 7 | MEDIUM | 2 | Next.js dashboard, chat UI with 4 component types, session list, magic-link signup via Resend, JWT cookie. |
| 5 | [ ] [Multi-Session & Crash Recovery](epic-05-multi-session-and-recovery.md) | 8 | MEDIUM | 1, 3 | Concurrent sessions per VM, volume-based recovery on supervisor restart, control plane reconciliation, hard cap 3/VM. |
| 6 | [ ] [Local Mode, Immutability, Memory](epic-06-local-immutability-memory.md) | 9 | MEDIUM | 1 | `mars run --local`, CLAUDE.md admin-only editing with supervisor restart, per-session memory capture with S3 sync. |
| 7 | [ ] [Developer Dogfood (Pedro)](epic-07-dev-dogfood.md) | 10 | LOW | 1–6 | Pedro deploys `pr-reviewer-agent.yaml` on `epic/agents-v2`. End-to-end smoke tests 1–7. Bug fixes. |
| 8 | [ ] [Operator Turnkey (Maat)](epic-08-operator-turnkey.md) | 11 | HIGH (product) | 3, 4 | `tracker-ops-assistant.yaml` template + onboarding wizard. Maat never sees YAML or CLI. |
| 9 | [ ] [Security Hardening & Launch](epic-09-security-and-launch.md) | 12–13 | LOW | 7, 8 | `docs/security.md` threat model, PreToolUse hook refinement, Maat setup call, v1.1 backlog capture, ship. |

## Counts

- Total: 10
- Done: 0
- In progress: 0
- Blocked: 0

---

## Next cycle: story decomposition brief

When you come back to generate stories per epic, the goal is:
- **Each story is half a day or less.**
- **Each story has a single testable outcome.**
- **Stories within an epic are sequenced** (1 → 2 → 3), with explicit dependencies noted when parallel.
- **Each story points at the specific files/functions to modify.**
- **Each story includes the smoke test that proves it works.**

Target: 3–6 stories per epic × 10 epics = 30–60 stories total. Ship 3–5 stories per day.
