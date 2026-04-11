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


def test_build_settings_emits_edit_write_multiedit_matcher():
    """Story 3.2 ships one matcher for Edit|Write|MultiEdit wired to
    deny-protected-edit.sh. The script is the single source of truth
    for which paths get denied."""
    settings = build_claude_code_settings(derive_policy(_config()))
    pretooluse = settings["hooks"]["PreToolUse"]
    edit_entries = [e for e in pretooluse if "Edit" in e["matcher"]]
    assert len(edit_entries) == 1
    entry = edit_entries[0]
    assert entry["matcher"] == "Edit|Write|MultiEdit"
    assert len(entry["hooks"]) == 1
    hook = entry["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"].endswith("deny-protected-edit.sh")
    assert hook["timeout"] == 10


def test_build_settings_emits_bash_matcher():
    """Story 3.2 ships one matcher for Bash wired to
    deny-secret-read-bash.sh."""
    settings = build_claude_code_settings(derive_policy(_config()))
    pretooluse = settings["hooks"]["PreToolUse"]
    bash_entries = [e for e in pretooluse if e["matcher"] == "Bash"]
    assert len(bash_entries) == 1
    hook = bash_entries[0]["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"].endswith("deny-secret-read-bash.sh")


def test_build_settings_uses_hooks_dir_argument():
    """Tests can stamp a different absolute path for the hook
    scripts (useful when the image layout changes)."""
    settings = build_claude_code_settings(
        derive_policy(_config()), hooks_dir="/etc/mars/hooks"
    )
    for entry in settings["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            assert hook["command"].startswith("/etc/mars/hooks/")


def test_build_settings_default_hooks_dir_is_container_path():
    """Default ``hooks_dir`` matches what the Mars container bakes."""
    settings = build_claude_code_settings(derive_policy(_config()))
    for entry in settings["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            assert hook["command"].startswith("/app/hooks/")


def test_build_settings_denylist_defaults_are_still_on_the_policy():
    """Regression guard: even though the rendered dict no longer
    embeds the denied paths / bash patterns (the hook scripts own
    those), the policy value object still documents them for
    introspection + future history-aware rendering."""
    policy = derive_policy(_config())
    assert policy.denied_edit_paths == DEFAULT_DENYLIST_EDIT_PATHS
    assert "CLAUDE.md" in policy.denied_edit_paths
    assert "AGENTS.md" in policy.denied_edit_paths
    assert policy.denied_bash_patterns == DEFAULT_DENYLIST_BASH_PATTERNS


def test_build_settings_is_json_serializable():
    """The Docker bake step dumps this to a file as JSON — round-trip
    must be lossless."""
    import json

    settings = build_claude_code_settings(derive_policy(_config(tools=["Bash"])))
    payload = json.dumps(settings)
    reparsed = json.loads(payload)
    assert reparsed["permissionMode"] == "acceptEdits"
    assert "PreToolUse" in reparsed["hooks"]
    # Matcher is a string, not a dict — matches Claude Code's real schema
    for entry in reparsed["hooks"]["PreToolUse"]:
        assert isinstance(entry["matcher"], str)


def test_build_settings_matches_baked_claude_code_settings_json():
    """The on-disk apps/mars-runtime/claude_code_settings.json must
    agree with what ``build_claude_code_settings`` generates for the
    default policy + default hooks_dir. Prevents the two layers from
    silently drifting."""
    import json
    from pathlib import Path

    baked = Path(__file__).resolve().parents[2] / "apps" / "mars-runtime" / "claude_code_settings.json"
    on_disk = json.loads(baked.read_text())
    generated = build_claude_code_settings(derive_policy(_config()))
    assert on_disk == generated, (
        "baked claude_code_settings.json does not match "
        "build_claude_code_settings(default policy)"
    )
