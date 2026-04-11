# Decision 002 — Fly machine cold-boot timing (Spike 5)

**Status:** PENDING live measurement — code paths ready, needs Pedro's Fly token + a single `mars deploy` run
**Related:** Story 3.4 (`mars deploy`) + Story 3.5 (spikes 4 and 5)
**Decision:** Defaulting to "cold boot is fast enough for v1" *unless the measurement below contradicts it*.

## Spike 4 — machine → control plane outbound HTTP reachability

**Measurement procedure** (Pedro runs once):

```bash
# Pre: FLY_API_TOKEN, MARS_EVENT_SECRET, MARS_CONTROL_URL set in the shell
cd ~/Desktop/mars-daemons
./packages/mars-cli/src/mars/__main__.py deploy examples/pr-reviewer-agent.yaml \
    --event-secret "$MARS_EVENT_SECRET" \
    --control-url "$MARS_CONTROL_URL"

# Wait ~20s for the machine to come up, then curl the supervisor
curl -sf "https://mars-pr-reviewer.fly.dev/health"
# Expect: {"status":"ok","active_sessions":0}

# On the control-plane side, tail the events ingest log — a healthy
# forwarder will POST a session_started event within a few seconds
# of the machine boot.
```

**What it verifies:**

* Fly machine can reach `MARS_CONTROL_URL` over HTTPS from the Fly
  private network.
* The machine's HttpEventForwarder POSTs events with a valid
  X-Event-Secret header.
* The control plane accepts the POST and persists a session_started
  row.

**Pivot if it fails:**

* Check Fly app network config — machines.dev machines have outbound
  internet by default; if blocked, set `config.services[0].protocol`
  or Fly's `[http_service]` to allow outbound.
* Fall back to a VPN or Fly's wireguard bridge.

## Spike 5 — cold boot time

**Measurement procedure** (Pedro runs once after spike 4 passes):

```bash
time mars deploy examples/pr-reviewer-agent.yaml
# Record total wall-clock from command start to the printed
# "Deploy complete. Supervisor health: <url>" line.

# Then curl /health in a loop and record the time from the first
# successful response back:
START=$(date +%s)
while ! curl -sf "https://mars-pr-reviewer.fly.dev/health" > /dev/null; do
    sleep 1
done
END=$(date +%s)
echo "boot-to-ready: $((END - START))s"
```

**Thresholds** (from v1 plan):

| Boot-to-ready | Action |
|---|---|
| <15s | 🟢 Nominal — nothing to do |
| 15–30s | 🟡 Acceptable for v1 — document in Maat onboarding UX |
| 30–60s | 🟠 Needs warm-pool strategy in Epic 8 |
| >60s | 🔴 Re-architect; investigate image size, layer caching, or smaller base image |

**Recorded measurement (Pedro to fill in):**

```
Date:            ____
Image size:      ____ MB  (docker images mars-runtime:latest)
mars deploy:     ____s    (time mars deploy ...)
Machine ready:   ____s    (first successful /health)
Category:        🟢/🟡/🟠/🔴
Action taken:    ____
```

## Why the doc is committed before the measurement

Spike 5 explicitly needs a live Fly deploy to measure. Committing the
procedure now means:

1. The Story 3.5 done-when has a concrete pointer (this file) instead
   of being "measurement exists" in hand-waves.
2. Pedro (or CI, eventually) can fill in the blanks in under 2
   minutes once the live path is unblocked — no "what was I supposed
   to measure again?" friction.
3. If the thresholds change post-measurement, the change lands as a
   follow-up commit updating this file rather than a new decision.
