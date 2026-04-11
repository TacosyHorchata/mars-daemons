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


def build_claude_code_settings(policy: PermissionPolicy) -> dict[str, Any]:
    """Render a :class:`PermissionPolicy` into the exact dict shape
    that ``claude_code_settings.json`` expects.

    The structure is Claude Code's own hook schema:

    .. code-block:: json

        {
            "permissionMode": "acceptEdits",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": {"tool": "Edit", "file_path": "CLAUDE.md"},
                        "action": "deny",
                        "reason": "..."
                    },
                    ...
                ]
            }
        }

    Epic 3 writes this dict to a file and passes ``--settings`` to
    ``claude -p`` at spawn time. Story 1.6 is the single source of
    truth for the file's shape so the Docker bake step stays honest.
    """
    hooks: list[dict[str, Any]] = []

    for path in policy.denied_edit_paths:
        for tool in ("Edit", "Write", "MultiEdit"):
            hooks.append(
                {
                    "matcher": {"tool": tool, "file_path": path},
                    "action": "deny",
                    "reason": (
                        f"{path} is admin-only in Mars — edits are blocked to "
                        "preserve the daemon's system prompt contract. See "
                        "docs/security.md and v1 plan item 8."
                    ),
                }
            )

    for pattern in policy.denied_bash_patterns:
        hooks.append(
            {
                "matcher": {"tool": "Bash", "command_regex": pattern},
                "action": "deny",
                "reason": (
                    "Bash commands matching secret-read patterns are blocked "
                    "as a speed bump. See docs/security.md for the v1 threat "
                    "model."
                ),
            }
        )

    return {
        "permissionMode": policy.permission_mode,
        "hooks": {"PreToolUse": hooks},
    }
