"""Registry + provider inference tests."""

from __future__ import annotations

import pytest

from mars_runtime.llm_client import base, get, infer_provider, register, registered


def test_load_all_registers_all_providers():
    base.load_all()
    names = registered()
    assert "anthropic" in names
    assert "azure_openai" in names
    assert "openai" in names
    assert "gemini" in names


def test_get_unknown_provider_raises():
    base.load_all()
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get("not_a_real_provider")


def test_infer_provider_from_model_prefix():
    base.load_all()
    assert infer_provider("claude-opus-4-5") == "anthropic"
    assert infer_provider("claude-sonnet-4-5") == "anthropic"
    # gpt-* / o1-* / o3-* → OpenAI direct (api.openai.com).
    # Azure deployments use custom names and require `provider: azure_openai`.
    assert infer_provider("gpt-5.4") == "openai"
    assert infer_provider("gpt-4o") == "openai"
    assert infer_provider("o1-preview") == "openai"
    assert infer_provider("gemini-1.5-pro") == "gemini"


def test_infer_provider_rejects_unknown():
    with pytest.raises(ValueError, match="cannot infer"):
        infer_provider("mystery-model")


def test_register_adds_provider():
    class _Fake:
        def chat(self, **kwargs):
            raise NotImplementedError

    register("fake_test_provider", lambda **_: _Fake())
    assert "fake_test_provider" in registered()
    assert isinstance(get("fake_test_provider"), _Fake)


def test_gemini_is_registered_but_unusable():
    base.load_all()
    with pytest.raises(NotImplementedError, match="not implemented"):
        get("gemini")


def test_name_collision_raises():
    """Two providers registering the same name with different factories is a bug."""
    from mars_runtime.llm_client import ProviderCollision

    def _f1(**_):
        return object()

    def _f2(**_):
        return object()

    register("collision_test_one", _f1)
    with pytest.raises(ProviderCollision, match="already registered"):
        register("collision_test_one", _f2)


def test_prefix_collision_raises():
    from mars_runtime.llm_client import ProviderCollision

    register(
        "prefix_test_a",
        lambda **_: object(),
        model_prefixes=["pfx-unique-xyz"],
    )
    with pytest.raises(ProviderCollision, match="already claimed"):
        register(
            "prefix_test_b",
            lambda **_: object(),
            model_prefixes=["pfx-unique-xyz"],
        )


def test_empty_prefix_raises():
    with pytest.raises(ValueError, match="empty model prefix"):
        register(
            "bad_provider_empty",
            lambda **_: object(),
            model_prefixes=[""],
        )


def test_empty_name_raises():
    with pytest.raises(ValueError, match="name cannot be empty"):
        register("", lambda **_: object())


def test_idempotent_reregister_same_factory():
    """Re-registering with the EXACT same factory must not raise."""
    base.load_all()
    # anthropic is already registered by load_all; call again with current
    # factory should be a no-op, not an error.
    current = base._PROVIDERS["anthropic"]
    register("anthropic", current, model_prefixes=["claude"])  # same factory, same prefix
    # still works
    assert "anthropic" in registered()


def test_infer_provider_rejects_non_string():
    with pytest.raises(ValueError, match="non-empty string"):
        infer_provider(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty string"):
        infer_provider("")
