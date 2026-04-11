"""Unit tests for :mod:`session.permissions` — the v1 fallback policy."""

from __future__ import annotations

from schema.agent import AgentConfig
from session.permissions import (
    DEFAULT_DENYLIST_BASH_PATTERNS,
    DEFAULT_DENYLIST_EDIT_PATHS,
    PermissionPolicy,
    build_claude_code_settings,
    derive_policy,
)


def _config(name: str = "policy-test", tools: list[str] | None = None) -> AgentConfig:
    return AgentConfig(
        name=name,
        description="policy test agent",
        runtime="claude-code",
        system_prompt_path="CLAUDE.md",
        tools=tools or [],
        workdir="/tmp",
    )


# ---------------------------------------------------------------------------
# derive_policy
# ---------------------------------------------------------------------------


def test_derive_policy_hardcodes_accept_edits_mode():
    policy = derive_policy(_config())
    assert policy.permission_mode == "acceptEdits"


def test_derive_policy_empty_tools_has_no_explicit_allowlist():
    policy = derive_policy(_config(tools=[]))
    assert policy.allowed_tools == ()
    assert policy.has_explicit_allowlist is False


def test_derive_policy_respects_config_tools_order():
    policy = derive_policy(_config(tools=["Bash", "Read", "Grep"]))
    assert policy.allowed_tools == ("Bash", "Read", "Grep")
    assert policy.has_explicit_allowlist is True


def test_derive_policy_always_includes_default_denylist():
    policy = derive_policy(_config(tools=["Bash"]))
    assert policy.denied_edit_paths == DEFAULT_DENYLIST_EDIT_PATHS
    assert policy.denied_bash_patterns == DEFAULT_DENYLIST_BASH_PATTERNS


def test_policy_is_frozen_and_hashable():
    """PermissionPolicy must be a value object so two daemons with the
    same config produce identical, comparable policies."""
    a = derive_policy(_config(tools=["Bash"]))
    b = derive_policy(_config(tools=["Bash"]))
    assert a == b
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# build_claude_code_settings
# ---------------------------------------------------------------------------


def test_build_settings_has_permission_mode_at_top_level():
    settings = build_claude_code_settings(derive_policy(_config()))
    assert settings["permissionMode"] == "acceptEdits"


def test_build_settings_emits_pretooluse_hook_per_denied_path_and_tool():
    settings = build_claude_code_settings(derive_policy(_config()))
    hooks = settings["hooks"]["PreToolUse"]
    # CLAUDE.md / AGENTS.md / claude_code_settings.json × Edit / Write / MultiEdit
    # = 3 paths × 3 tools = 9 file-edit hooks
    edit_hooks = [
        h for h in hooks if h["matcher"].get("tool") in {"Edit", "Write", "MultiEdit"}
    ]
    assert len(edit_hooks) == 3 * 3
    for h in edit_hooks:
        assert h["action"] == "deny"
        assert h["matcher"]["file_path"] in DEFAULT_DENYLIST_EDIT_PATHS
        assert h["matcher"]["tool"] in {"Edit", "Write", "MultiEdit"}


def test_build_settings_emits_one_bash_hook_per_denied_pattern():
    settings = build_claude_code_settings(derive_policy(_config()))
    hooks = settings["hooks"]["PreToolUse"]
    bash_hooks = [h for h in hooks if h["matcher"].get("tool") == "Bash"]
    assert len(bash_hooks) == len(DEFAULT_DENYLIST_BASH_PATTERNS)
    patterns = {h["matcher"]["command_regex"] for h in bash_hooks}
    assert patterns == set(DEFAULT_DENYLIST_BASH_PATTERNS)


def test_build_settings_denies_claude_md_edits():
    """Regression guard for v1 plan item 8 (CLAUDE.md immutability).
    Removing this hook would silently allow daemons to rewrite their
    own system prompts."""
    settings = build_claude_code_settings(derive_policy(_config()))
    hooks = settings["hooks"]["PreToolUse"]
    matching = [
        h
        for h in hooks
        if h["matcher"].get("tool") == "Edit"
        and h["matcher"].get("file_path") == "CLAUDE.md"
    ]
    assert len(matching) == 1
    assert matching[0]["action"] == "deny"


def test_build_settings_denies_env_read_patterns():
    """Regression guard for v1 plan item 10 (secret-read speed bump)."""
    settings = build_claude_code_settings(derive_policy(_config()))
    hooks = settings["hooks"]["PreToolUse"]
    env_hooks = [
        h
        for h in hooks
        if h["matcher"].get("tool") == "Bash"
        and h["matcher"].get("command_regex") == r"\benv\b"
    ]
    assert len(env_hooks) == 1
    assert env_hooks[0]["action"] == "deny"


def test_build_settings_is_json_serializable():
    """The Epic 3 Docker bake step dumps this to a file as JSON, so
    the dict must not carry any non-JSON-native types."""
    import json

    settings = build_claude_code_settings(derive_policy(_config(tools=["Bash"])))
    # Round-trip through json.dumps / loads to prove no tuples/frozensets leak
    payload = json.dumps(settings)
    reparsed = json.loads(payload)
    assert reparsed["permissionMode"] == "acceptEdits"
    assert "PreToolUse" in reparsed["hooks"]


def test_policies_for_different_tool_sets_produce_distinct_settings():
    s_empty = build_claude_code_settings(derive_policy(_config(tools=[])))
    s_bash = build_claude_code_settings(derive_policy(_config(tools=["Bash"])))
    # The settings.json shape does NOT include the allowlist (that goes
    # via --allowed-tools on the command line), so the hook lists here
    # should actually be identical regardless of tools.
    assert s_empty["hooks"] == s_bash["hooks"]
    # But the policy itself is distinct:
    assert derive_policy(_config(tools=[])) != derive_policy(
        _config(tools=["Bash"])
    )
