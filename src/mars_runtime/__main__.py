"""mars-runtime entry point — thin dispatcher.

Session usage:
  python -m mars_runtime <agent.yaml>          # start a new session
  python -m mars_runtime --resume <id>         # resume an existing session
  python -m mars_runtime --list                # list recent sessions

File transfer (host ↔ sandbox, USER only — the agent has no access):
  python -m mars_runtime push <local> <dest>
  python -m mars_runtime pull <src>   <local>

Architecture map:

  __main__.py            thin dispatcher (this file)
  cli/
    files.py             host-side push/pull
    run.py               session lifecycle (parse, load, broker spawn)
  broker/                credentialed process: env, hardening, RPC forward
  worker/                sandboxed agent-loop process, no credentials
  runtime/               agent_loop — the tool-use turn loop
  storage/               session snapshots + git workspace
  providers/             LLM SDK wrappers (Anthropic, OpenAI, Azure, Gemini)
  tools/                 base + registry + builtin/
  api.py                 stable embedding surface for library consumers

For library embedding, import `mars_runtime.api` — not this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .broker import env as broker_env
from .broker import hardening as broker_hardening
from .cli import files as _cli_files
from .cli import run as _cli_run


def _ingest_secrets_fd() -> None:
    """Legacy alias — see `broker.env.ingest_secrets_fd`. Kept so tests
    that patch `mars_runtime.__main__._ingest_secrets_fd` still work."""
    broker_env.ingest_secrets_fd()


def _harden_broker() -> None:
    """Legacy alias — see `broker.hardening.harden_broker`."""
    broker_hardening.harden_broker()


def main(argv: list[str] | None = None) -> int:
    argv_list = list(argv) if argv is not None else sys.argv[1:]

    # File-transfer subcommands are host-side only; they do not reach the
    # runtime hardening path or the broker/worker split.
    # If the user has a yaml literally named "push" or "pull", treat it as
    # the yaml path (preserves the original entrypoint contract).
    if argv_list and argv_list[0] in ("push", "pull") and not Path(argv_list[0]).is_file():
        if argv_list[0] == "push":
            return _cli_files.cmd_push(argv_list[1:])
        return _cli_files.cmd_pull(argv_list[1:])

    # Session flow. Test hooks: the legacy __main__ names _ingest_secrets_fd
    # and _harden_broker are injected into cli.run.main so tests patching
    # them still intercept startup.
    return _cli_run.main(
        argv_list,
        ingest_secrets_fd=_ingest_secrets_fd,
        harden_broker=_harden_broker,
    )


if __name__ == "__main__":
    sys.exit(main())
