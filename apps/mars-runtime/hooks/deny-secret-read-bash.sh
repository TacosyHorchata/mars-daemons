#!/usr/bin/env bash
# Mars PreToolUse hook — deny Bash commands that try to read env secrets.
#
# Wired in ``apps/mars-runtime/claude_code_settings.json`` for the
# ``Bash`` matcher. Mirrors the permissions-layer denylist in
# ``apps/mars-runtime/src/session/permissions.py``.
#
# This is a SPEED BUMP, not a sandbox. A determined daemon can work
# around it (e.g. ``python3 -c 'import os; print(os.environ[\"X\"])'``).
# The v1 threat model in ``docs/security.md`` (Epic 9) states this
# explicitly: Mars assumes the daemon runs code Pedro wrote using
# keys Pedro owns. The speed bump catches accidental exfiltration
# and shallow prompt-injection attempts.
#
# Matching strategy
# -----------------
# Only checks commands at *command position* — the first token of the
# whole command, or the first token after a shell separator
# (``;``, ``&&``, ``||``, ``|``, ``&``). This avoids false positives
# like ``grep -r env /src`` or ``sed 's/env/ENV/g' file`` where ``env``
# appears as an argument, not as a command being executed.
#
# Denylisted commands at command position:
#
#   * ``env``     — dumps the full environment
#   * ``printenv`` — dumps one or all env vars
#   * bare ``set`` (no arguments) — Bash dumps all vars + functions
#
# Denylisted pattern (anywhere, expansion-level):
#
#   * ``echo $...`` — the ``$`` expansion is the exfiltration, the
#     ``echo`` is just the output channel. Catches ``echo $FOO``,
#     ``echo  $BAR`` (extra whitespace), etc.
#
# Pure bash + python3 — no jq dep.

set -euo pipefail

input="$(cat)"
command="$(python3 -c 'import sys, json; print(json.loads(sys.stdin.read() or "{}").get("tool_input", {}).get("command", ""))' <<< "$input" || true)"

if [[ -z "$command" ]]; then
    exit 0
fi

deny() {
    echo "Mars: Bash command matches a secret-read denylist pattern — blocked. (docs/security.md v1 threat model)" >&2
    exit 2
}

# Expansion-level check: echo $... anywhere in the command.
if [[ "$command" =~ echo[[:space:]]+\$ ]]; then
    deny
fi

# Command-position check: normalize shell separators to newlines so
# each segment's first token is the command about to execute, then
# inspect each segment.
#
# `tr` replaces ;, |, & with newlines — `&&` and `||` collapse to
# two newlines which is fine (empty segments are skipped).
normalized="$(printf '%s' "$command" | tr ';|&' '\n\n\n')"

while IFS= read -r segment; do
    # Strip leading and trailing whitespace
    trimmed="${segment#"${segment%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    if [[ -z "$trimmed" ]]; then
        continue
    fi
    # First word of this segment
    first="${trimmed%%[[:space:]]*}"
    case "$first" in
        env|printenv)
            deny
            ;;
        set)
            # Bare `set` (no arguments) dumps variables.
            # `set -euo pipefail` and `set foo bar` are fine.
            if [[ "$trimmed" == "set" ]]; then
                deny
            fi
            ;;
    esac
done <<< "$normalized"

exit 0
