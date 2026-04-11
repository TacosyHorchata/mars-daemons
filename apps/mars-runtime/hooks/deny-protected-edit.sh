#!/usr/bin/env bash
# Mars PreToolUse hook — deny edits to admin-only files.
#
# Wired in ``apps/mars-runtime/claude_code_settings.json`` for the
# ``Edit|Write|MultiEdit`` matcher. Claude Code invokes this script
# with the tool-call JSON on stdin. Exit code 2 tells Claude Code
# "stop, don't do this" and surfaces the stderr message to the model.
#
# Protected files (see ``apps/mars-runtime/src/session/permissions.py``
# for the single source of truth):
#
#   * CLAUDE.md, AGENTS.md — the daemon's system prompt contract.
#     v1 plan item 8: editable ONLY by admin via the web UI prompt
#     editor, NEVER by the daemon itself. A daemon rewriting its own
#     prompt is a hard security failure.
#
#   * claude_code_settings.json — the file that defines THIS hook.
#     Self-preservation: a daemon that can edit the settings file can
#     turn off the denies and then edit anything else.
#
# Pure bash + python3 (which is already in the image) — no jq dep.

set -euo pipefail

input="$(cat)"
file_path="$(python3 -c 'import sys, json; print(json.loads(sys.stdin.read() or "{}").get("tool_input", {}).get("file_path", ""))' <<< "$input" || true)"

if [[ -z "$file_path" ]]; then
    # No file_path — not something we care about.
    exit 0
fi

basename="$(basename "$file_path" 2>/dev/null || echo '')"

case "$basename" in
    CLAUDE.md|AGENTS.md|claude_code_settings.json)
        echo "Mars: '$basename' is admin-only and cannot be edited by a daemon (see docs/security.md / v1 plan item 8)." >&2
        exit 2
        ;;
esac

exit 0
