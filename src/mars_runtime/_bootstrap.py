"""Bootstrap wrapper — scrubs LLM provider secrets from the broker's
execve environment so `/proc/<broker_pid>/environ` never shows them.

Why this exists:

`/proc/<pid>/environ` is a kernel-frozen snapshot of the env passed to
`execve()`. Calling `os.environ.pop()` or `unsetenv()` from Python only
updates the process's in-memory env; it does NOT rewrite the kernel
snapshot. So an attacker with same-UID procfs access (e.g., an LLM tool
running `cat /proc/$PPID/environ`) can still recover any secret that
Docker injected when the container started.

This wrapper closes that hole. It:

1. Reads provider secrets from the inherited env
2. Builds a CLEAN env (no secrets)
3. Writes the secrets to a pipe and leaves the read end open across exec
4. `execvpe`'s the real broker (`python -m mars_runtime`) with the clean
   env plus `MARS_SECRETS_FD` pointing at the pipe
5. The broker reads its secrets from the FD, then drains and closes it

Because execve replaces the process image in-place, the kernel rewrites
`/proc/<pid>/environ` to reflect the CLEAN env. A same-UID reader now
sees nothing sensitive. The pipe buffer is freed when the broker closes
the FD; there is no on-disk artifact.

Docker CMD wires tini → `python -m mars_runtime._bootstrap`. Local /
pytest runs skip bootstrap entirely; the broker falls back to reading
`os.environ` directly when `MARS_SECRETS_FD` is not set.
"""

from __future__ import annotations

import json
import os
import sys


# Kept in sync with __main__._ALWAYS_STRIP_EXACT. These are the env vars
# the registered provider clients (anthropic, openai_direct, azure_openai,
# gemini) consume at construction time.
PROVIDER_SECRET_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",  # custom base URL — may reveal a private Azure resource host
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_API_VERSION",
        "OPENAI_ORGANIZATION",
        "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
    }
)


def _broker_argv(forwarded: list[str]) -> list[str]:
    return [sys.executable, "-m", "mars_runtime", *forwarded]


def main(argv: list[str] | None = None) -> int:
    forwarded = argv if argv is not None else sys.argv[1:]
    broker_cmd = _broker_argv(forwarded)

    secrets = {k: os.environ[k] for k in PROVIDER_SECRET_VARS if k in os.environ}

    if not secrets:
        # Nothing to protect — pass the original env through and hand off.
        os.execvpe(sys.executable, broker_cmd, os.environ)
        return 0  # unreachable

    clean_env = {k: v for k, v in os.environ.items() if k not in PROVIDER_SECRET_VARS}

    # os.pipe() returns (read_fd, write_fd). The read end must survive
    # execve so the broker can drain it. write end closes here.
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)

    try:
        payload = json.dumps(secrets).encode("utf-8")
        os.write(write_fd, payload)
    finally:
        os.close(write_fd)

    clean_env["MARS_SECRETS_FD"] = str(read_fd)

    os.execvpe(sys.executable, broker_cmd, clean_env)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
