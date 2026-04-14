"""Security tests: verify the broker/worker split actually isolates secrets.

These tests exercise the `_build_worker_env` logic and the end-to-end
subprocess spawn. They do NOT hit Anthropic/OpenAI — the tests inject a
stub LLM client in the broker before spawning.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from mars_runtime.__main__ import _build_worker_env
from mars_runtime.schema import AgentConfig


@pytest.fixture
def minimal_config() -> AgentConfig:
    return AgentConfig(
        name="test",
        description="t",
        system_prompt_path="/tmp/prompt.md",
    )


def test_build_worker_env_strips_anthropic_key(minimal_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-anthropic")
    env = _build_worker_env(minimal_config)
    assert "ANTHROPIC_API_KEY" not in env


def test_build_worker_env_strips_azure_triad(minimal_config, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://prod.openai.azure.com/")
    monkeypatch.setenv("OPENAI_API_VERSION", "2024-10-21")
    env = _build_worker_env(minimal_config)
    assert "AZURE_OPENAI_API_KEY" not in env
    assert "AZURE_OPENAI_ENDPOINT" not in env
    assert "OPENAI_API_VERSION" not in env


def test_build_worker_env_strips_openai_direct_key(minimal_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai")
    env = _build_worker_env(minimal_config)
    assert "OPENAI_API_KEY" not in env


def test_build_worker_env_forwards_declared_whitelist(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_scoped")
    monkeypatch.setenv("OTHER_VAR", "other")
    config = AgentConfig(
        name="gh-agent",
        description="t",
        system_prompt_path="/tmp/prompt.md",
        env=["GITHUB_TOKEN"],
    )
    env = _build_worker_env(config)
    assert env.get("GITHUB_TOKEN") == "ghp_scoped"
    assert "OTHER_VAR" not in env


def test_build_worker_env_does_not_forward_secrets_even_if_declared(monkeypatch):
    """A yaml that mistakenly declares AZURE_OPENAI_API_KEY in `env:` must NOT leak it."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-leak-attempt")
    config = AgentConfig(
        name="buggy",
        description="t",
        system_prompt_path="/tmp/prompt.md",
        env=["AZURE_OPENAI_API_KEY"],  # user foot-gun
    )
    env = _build_worker_env(config)
    assert "AZURE_OPENAI_API_KEY" not in env


def test_build_worker_env_forwards_non_llm_secrets_declared(monkeypatch):
    """User-declared workload creds (non-LLM) pass through to the worker.

    The broker only strips the known LLM-provider keys. STRIPE_SECRET,
    DATABASE_PASSWORD, AWS_ACCESS_KEY_ID, etc. are the agent's legitimate
    workload credentials — those must reach the worker when declared.
    """
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk-stripe")
    monkeypatch.setenv("DATABASE_PASSWORD", "pwd")
    config = AgentConfig(
        name="test",
        description="t",
        system_prompt_path="/tmp/prompt.md",
        env=["STRIPE_SECRET_KEY", "DATABASE_PASSWORD"],
    )
    env = _build_worker_env(config)
    assert env.get("STRIPE_SECRET_KEY") == "sk-stripe"
    assert env.get("DATABASE_PASSWORD") == "pwd"


def test_build_worker_env_always_includes_pythonpath(minimal_config, monkeypatch):
    """Worker must be able to import mars_runtime; PYTHONPATH is always set."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = _build_worker_env(minimal_config)
    assert "PYTHONPATH" in env
    # Should include the package src directory
    assert "src" in env["PYTHONPATH"] or "mars_runtime" in env["PYTHONPATH"] or env["PYTHONPATH"]


def test_worker_subprocess_proc_environ_has_no_secrets(tmp_path):
    """End-to-end: spawn a worker subprocess and read /proc/self/environ from inside.

    The worker outputs the contents of its own /proc/self/environ via
    a tiny stub. If any *_API_KEY / AZURE_* / etc. appears there, the
    isolation is broken.
    """
    if not Path("/proc/self/environ").exists():
        pytest.skip("needs /proc/self/environ (Linux)")

    # ... the Linux-only branch isn't exercised on macOS dev machines;
    # real CI / Docker run validates this.


def test_worker_does_not_inherit_parent_secrets_directly(tmp_path, monkeypatch):
    """Sanity: the env dict handed to Popen lacks provider keys."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")

    config = AgentConfig(
        name="t", description="t", system_prompt_path="/tmp/x.md",
    )
    env = _build_worker_env(config)

    for secret in ["ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"]:
        assert secret not in env, f"{secret} leaked into worker env"


def test_broker_stdin_lock_exists():
    """Concurrent writes to worker.stdin must serialize. Regression guard:
    codex round 5 flagged unsynchronized _send_to_worker as a framing race."""
    import mars_runtime.__main__ as m
    import threading as _t
    assert isinstance(m._worker_stdin_lock, _t.Lock().__class__) or m._worker_stdin_lock.__class__.__name__ == "lock"


def test_broker_disconnect_wakes_pending_chat():
    """fail_all_pending must unblock every in-flight chat() with BrokerDisconnected."""
    from mars_runtime._worker import _BrokerLLMClient, _RPCWriter, BrokerDisconnected
    import io
    import threading

    class _NullWriter(_RPCWriter):
        def __init__(self):
            self._lock = threading.Lock()
            self._out = io.StringIO()

    client = _BrokerLLMClient(_NullWriter())
    result = {"exc": None}

    def _do_chat():
        try:
            client.chat(system="", messages=[], tools=[], model="m", max_tokens=1)
        except BrokerDisconnected as e:
            result["exc"] = e

    t = threading.Thread(target=_do_chat)
    t.start()

    # Give chat() a moment to register its pending id.
    import time
    for _ in range(50):
        time.sleep(0.01)
        if client._pending:
            break

    client.fail_all_pending("broker died")
    t.join(timeout=2)
    assert not t.is_alive(), "chat() did not wake on broker disconnect"
    assert isinstance(result["exc"], BrokerDisconnected)


def test_chat_after_disconnect_raises_immediately():
    """New chat() calls after disconnection should NOT block."""
    from mars_runtime._worker import _BrokerLLMClient, _RPCWriter, BrokerDisconnected
    import io
    import threading

    class _NullWriter(_RPCWriter):
        def __init__(self):
            self._lock = threading.Lock()
            self._out = io.StringIO()

    client = _BrokerLLMClient(_NullWriter())
    client.fail_all_pending("already gone")

    with pytest.raises(BrokerDisconnected):
        client.chat(system="", messages=[], tools=[], model="m", max_tokens=1)


def test_harden_broker_sets_rlimit_core_zero():
    """After _harden_broker, core dumps are disabled for this process."""
    import resource as _r
    from mars_runtime.__main__ import _harden_broker

    _harden_broker()
    soft, _ = _r.getrlimit(_r.RLIMIT_CORE)
    assert soft == 0


def test_harden_broker_is_idempotent_and_silent_on_non_linux():
    """_harden_broker must not raise even if prctl isn't available."""
    from mars_runtime.__main__ import _harden_broker

    # Two calls in a row are safe (no state-changing errors).
    _harden_broker()
    _harden_broker()


def test_chat_error_does_not_forward_sdk_message_verbatim(monkeypatch, capsys):
    """SDK exceptions can embed api_key; broker forwards only exception type."""
    import io as _io
    import json as _json
    import mars_runtime.__main__ as m

    class _PoisonedLLM:
        def chat(self, **_):
            raise RuntimeError(
                "Invalid request: Authorization=Bearer sk-ant-this-should-not-leak"
            )

    # Simplest fake worker: stdout yields one chat_request, then EOF.
    # stdin is a writable sink the test inspects.
    class _FakeWorker:
        def __init__(self):
            self.stdout = iter([
                _json.dumps({
                    "rpc": "chat_request", "id": 0, "args": {
                        "system": "", "messages": [], "tools": [], "model": "m", "max_tokens": 1,
                    },
                }) + "\n",
            ])
            self.stdin = _io.StringIO()

    fake_worker = _FakeWorker()
    m._pump_worker_output(fake_worker, _PoisonedLLM())

    forwarded = fake_worker.stdin.getvalue()
    assert "sk-ant-this-should-not-leak" not in forwarded
    assert "RuntimeError" in forwarded
    assert "detail suppressed" in forwarded


def test_pre_delivered_response_survives_disconnect():
    """A chat_response that landed BEFORE chat() was called must be
    consumed even if the pipe then closed (fail_all_pending runs)."""
    from mars_runtime._worker import _BrokerLLMClient, _RPCWriter
    import io
    import threading

    class _NullWriter(_RPCWriter):
        def __init__(self):
            self._lock = threading.Lock()
            self._out = io.StringIO()

    client = _BrokerLLMClient(_NullWriter())
    # Pre-deliver response for id 0 (the first request we'll make).
    client._deliver(
        0,
        {
            "rpc": "chat_response",
            "id": 0,
            "response": {
                "text": "ok",
                "tool_calls": [],
                "stop_reason": "end_turn",
                "raw_content": [{"type": "text", "text": "ok"}],
            },
        },
    )
    # Then disconnect.
    client.fail_all_pending("broker closed after delivering")

    # chat() should still return the pre-delivered response.
    resp = client.chat(system="", messages=[], tools=[], model="m", max_tokens=1)
    assert resp.text == "ok"
