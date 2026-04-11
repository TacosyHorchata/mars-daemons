"""Permission policy layer for the Mars runtime.

v1 ships with the **static allowlist + PreToolUse denylist** fallback
confirmed in ``spikes/03-permission-roundtrip.md``. The real mid-session
human-in-loop round-trip (user clicks "Approve" on a pending tool call)
is deferred to v1.1 — Claude Code's stream-json wire schema for
pending-permission events is not yet stable and we did not want to
block Epic 1 on an external dependency.

Three layers compose the v1 policy:

1. **Launch-time allowlist.** The supervisor passes
   ``--allowed-tools`` to ``claude -p`` using the ``tools`` field of
   the daemon's :class:`AgentConfig`. A daemon with no explicit list
   inherits the runtime default allowlist; the supervisor does not
   inject one on its behalf.

2. **Permission mode.** Always ``acceptEdits`` in v1 (see
   :func:`session.claude_code.build_claude_command`). This auto-approves
   file edits and still prompts on high-risk tools, but since v1 runs
   headless the prompts never show a TTY — they surface as
   ``permission_denials`` entries on the final
   :class:`events.types.SessionEnded` event.

3. **PreToolUse hook denylist.** Baked into
   ``claude_code_settings.json`` at image build time (Epic 3). The
   hooks are Mars's last line of defence and enforce the
   non-negotiables: no edits to ``CLAUDE.md`` / ``AGENTS.md`` (prompt
   immutability, see v1 plan item 8), no ``bash`` commands matching
   secret-read patterns (``env``, ``printenv``, ``echo $``).

This module produces a :class:`PermissionPolicy` value object from an
:class:`AgentConfig` and can serialize it as the exact dict shape
``claude_code_settings.json`` expects. Story 1.6 covers derivation and
serialization; Epic 3 wires the serialized dict into the Docker image
bake step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schema.agent import AgentConfig

__all__ = [
    "DEFAULT_DENYLIST_BASH_PATTERNS",
    "DEFAULT_DENYLIST_EDIT_PATHS",
    "PermissionPolicy",
    "build_claude_code_settings",
    "derive_policy",
]


#: Files that must remain immutable to every daemon. Editing any of
#: these would compromise either the daemon's system prompt contract
#: (CLAUDE.md / AGENTS.md) or Mars's permission configuration itself
#: (claude_code_settings.json).
DEFAULT_DENYLIST_EDIT_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "claude_code_settings.json",
)

#: Bash command *regex* patterns that PreToolUse hooks must block.
#: These cover the most common ways an agent could ex-filtrate a
#: forwarded secret out of its env. Not watertight — the v1 threat
#: model (docs/security.md, Epic 9) acknowledges the residual risk and
#: treats these hooks as a "speed bump" rather than a full sandbox.
DEFAULT_DENYLIST_BASH_PATTERNS: tuple[str, ...] = (
    r"\benv\b",
    r"\bprintenv\b",
    r"\becho\s+\$",
    r"\bset\s*$",
)


@dataclass(frozen=True)
class PermissionPolicy:
    """A concrete, inspectable permission policy for one session.

    This is the v1 fallback described above: a value object
    summarizing what the daemon is allowed to do, which tools it
    cannot touch, and which file paths / bash patterns the
    PreToolUse hook layer blocks.
    """

    permission_mode: str
    allowed_tools: tuple[str, ...]
    denied_edit_paths: tuple[str, ...]
    denied_bash_patterns: tuple[str, ...]

    @property
    def has_explicit_allowlist(self) -> bool:
        """Whether the daemon config provided an explicit tools list.

        When ``False``, Mars spawns without ``--allowed-tools`` and
        the runtime's default tool set applies. When ``True``, only
        :attr:`allowed_tools` are reachable.
        """
        return bool(self.allowed_tools)


def derive_policy(config: AgentConfig) -> PermissionPolicy:
    """Build a :class:`PermissionPolicy` from an :class:`AgentConfig`.

    v1 hard-codes ``permission_mode = "acceptEdits"``; the denylist is
    always the default set. Only the allowlist varies per daemon.
    """
    return PermissionPolicy(
        permission_mode="acceptEdits",
        allowed_tools=tuple(config.tools),
        denied_edit_paths=DEFAULT_DENYLIST_EDIT_PATHS,
        denied_bash_patterns=DEFAULT_DENYLIST_BASH_PATTERNS,
    )


def build_claude_code_settings(
    policy: PermissionPolicy,
    *,
    hooks_dir: str = "/app/hooks",
) -> dict[str, Any]:
    """Render a :class:`PermissionPolicy` into Claude Code's real
    ``settings.json`` shape as verified against the 2.1.x hook docs.

    Claude Code's PreToolUse hook schema (confirmed for 2.1.101) is:

    .. code-block:: json

        {
            "permissionMode": "acceptEdits",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write|MultiEdit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/app/hooks/deny-protected-edit.sh",
                                "timeout": 10
                            }
                        ]
                    }
                ]
            }
        }

    The ``matcher`` field is a tool-name filter
    (exact string, ``|``-separated list, or regex). The deny decision
    is produced by the command hook itself — the script reads the
    tool-call JSON from stdin and returns exit code 2 (blocking) with
    a stderr message that Claude Code surfaces back to the model.

    Story 3.2 ships the canonical scripts at
    ``apps/mars-runtime/hooks/deny-protected-edit.sh`` and
    ``apps/mars-runtime/hooks/deny-secret-read-bash.sh``. The Mars
    image copies them to ``hooks_dir`` (default ``/app/hooks``) and
    this function stamps that absolute path into the rendered dict so
    the two layers never drift.

    Note: the ``denied_edit_paths`` and ``denied_bash_patterns`` fields
    on the policy are kept for documentation / introspection, but the
    rendered settings dict does NOT embed them — the per-path / per-
    pattern logic lives inside the hook scripts. They are the single
    source of truth; this function only wires paths to names.
    """
    # The earlier (1.6) version inlined per-path matchers into the
    # settings JSON. That shape was not the real Claude Code schema.
    # We intentionally reference the policy's denied lists via the
    # script-side docstrings instead of embedding them here, so the
    # policy value object still documents the contract.
    _ = policy.denied_edit_paths  # documentation-only reference
    _ = policy.denied_bash_patterns

    return {
        "permissionMode": policy.permission_mode,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|MultiEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hooks_dir}/deny-protected-edit.sh",
                            "timeout": 10,
                        }
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hooks_dir}/deny-secret-read-bash.sh",
                            "timeout": 10,
                        }
                    ],
                },
            ]
        },
    }
