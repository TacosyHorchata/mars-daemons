"""Broker-side env handling: LLM-provider secret ingest + worker env scrub.

Two distinct concerns share this module because they both operate on the
broker's environment:

1. `ingest_secrets_fd()` — drains the pipe FD our `_bootstrap` wrapper
   left behind with provider secrets, and re-injects them into
   `os.environ` (memory-only; /proc/<pid>/environ stays clean).
2. `build_worker_env()` — constructs the scrubbed env handed to the
   worker subprocess. Strips LLM-provider secrets unconditionally, then
   forwards only PATH / PYTHONPATH / locale + AgentConfig.env names.

Kept in the `broker/` subpackage because both are lifecycle concerns of
the credentialed broker process; the worker never imports either.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..schema import AgentConfig


# LLM-provider env vars that must NEVER reach the worker, even if the
# user accidentally declares them in `AgentConfig.env`. This is the list
# of things the registered provider clients consume at construction time.
# Anything else the user declares in `env:` is their own workload
# credential (GITHUB_TOKEN, AWS_ACCESS_KEY_ID, DATABASE_URL, ...) and is
# forwarded normally.
ALWAYS_STRIP_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",  # custom base URL may reveal a private Azure host
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_API_VERSION",
        "OPENAI_ORGANIZATION",
        "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
    }
)


def ingest_secrets_fd() -> None:
    """If `_bootstrap` handed us secrets via a pipe FD, drain it and put
    them into `os.environ` so LLM SDK constructors find them.

    No-op if MARS_SECRETS_FD is unset (local dev / pytest path — broker
    reads os.environ directly like before bootstrap existed).
    """
    fd_str = os.environ.pop("MARS_SECRETS_FD", None)
    if fd_str is None:
        return
    try:
        fd = int(fd_str)
    except ValueError:
        return
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not data:
        return
    try:
        secrets = json.loads(data)
    except json.JSONDecodeError:
        return
    if not isinstance(secrets, dict):
        return
    for k, v in secrets.items():
        if isinstance(k, str) and isinstance(v, str):
            os.environ[k] = v


def build_worker_env(config: AgentConfig) -> dict[str, str]:
    """Construct the env handed to the worker subprocess.

    Strategy: start from empty, forward only PATH / PYTHONPATH / locale
    plus explicitly-declared `AgentConfig.env` names. Never forward
    secrets matched by ALWAYS_STRIP_EXACT — even if the user accidentally
    declared them.

    PYTHONPATH is forwarded (and augmented with this package's src root)
    so `python -m mars_runtime.worker` resolves even when mars-runtime
    is not installed site-wide (editable dev, pytest, etc.).
    """
    allowlist = {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}
    clean: dict[str, str] = {}
    for k in allowlist:
        if k in os.environ:
            clean[k] = os.environ[k]

    # src/ parent — `src/mars_runtime/broker/env.py` → parents[2] = `src/`.
    pkg_root = str(Path(__file__).resolve().parents[2])
    existing_pp = os.environ.get("PYTHONPATH", "")
    clean["PYTHONPATH"] = (
        f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
    )

    for name in config.env:
        if name in ALWAYS_STRIP_EXACT:
            continue
        if name in os.environ:
            clean[name] = os.environ[name]
    return clean
