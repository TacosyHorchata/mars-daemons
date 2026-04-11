#!/usr/bin/env bash
# spikes/02-stream-json-capture.sh
#
# Spike 2: Capture canonical stream-json fixture from Claude Code.
#
# Runs a minimal, deterministic session that exercises the full canonical
# event sequence we need to parse in Epic 1:
#
#   system.init → assistant(tool_use) → user(tool_result) → assistant(text) → result
#
# and writes it to tests/contract/fixtures/stream_json_sample.jsonl.
#
# The filter does two things:
#   1. Drops `system.hook_started` / `system.hook_response` events — these
#      come from user-global `~/.claude/settings.json` hooks (e.g. cmux) and
#      will NOT exist in a clean Mars Fly.io container.
#   2. Strips a cmux-specific `\n[rerun: bN]` suffix that leaks into
#      child-process Bash tool_result stdout when running under Claude Code.
#      In a Mars production container this suffix does not exist.
#
# Pinned Claude Code version at capture: 2.1.101
#
# Usage:
#   ./spikes/02-stream-json-capture.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="$REPO_ROOT/tests/contract/fixtures/stream_json_sample.jsonl"
mkdir -p "$(dirname "$FIXTURE")"

PROMPT="Use the Bash tool to run exactly 'echo hello from mars spike' and then reply with one short sentence confirming what it printed."

env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_EXECPATH -u CMUX_CLAUDE_PID \
  claude -p "$PROMPT" \
    --output-format stream-json \
    --verbose \
    --allowed-tools Bash \
    --permission-mode acceptEdits \
  | python3 -c '
import json, re, sys

RERUN = re.compile(r"\n\[rerun: b\d+\]$")

def clean(ev: dict) -> dict | None:
    t = ev.get("type")
    sub = ev.get("subtype", "")
    # Drop user-global hook noise — not present in clean Mars container
    if t == "system" and sub.startswith("hook_"):
        return None
    # Strip cmux rerun artifact from Bash tool_result content
    if t == "user":
        for c in ev.get("message", {}).get("content", []) or []:
            if c.get("type") == "tool_result" and isinstance(c.get("content"), str):
                c["content"] = RERUN.sub("", c["content"])
    return ev

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        sys.stderr.write(f"skip non-json line: {line[:80]!r}\n")
        continue
    ev = clean(ev)
    if ev is None:
        continue
    sys.stdout.write(json.dumps(ev) + "\n")
' > "$FIXTURE"

echo "=== captured fixture ==="
wc -l "$FIXTURE"
echo "=== event sequence ==="
python3 -c "
import json
with open('$FIXTURE') as f:
    for line in f:
        ev = json.loads(line)
        t = ev.get('type')
        sub = ev.get('subtype', '')
        label = t if not sub else f'{t}.{sub}'
        if t == 'assistant':
            blocks = [b.get('type') for b in ev['message']['content']]
            label += f' blocks={blocks}'
        if t == 'user':
            blocks = [b.get('type') for b in ev['message']['content']]
            label += f' blocks={blocks}'
        print(label)
"
