"""Entry point: python -m mars_runtime ./agent.yaml

Loads agent.yaml, cds into workdir, constructs Anthropic client + tool
registry, runs the agent loop reading user turns from stdin.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .agent import run
from .llm_client import AnthropicClient
from .schema import AgentConfig
from .tools import ToolRegistry, load_all


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m mars_runtime <agent.yaml>", file=sys.stderr)
        return 2

    config = AgentConfig.from_yaml_file(sys.argv[1])

    workdir = Path(config.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)

    load_all()
    tools = ToolRegistry(config.tools or None)
    llm = AnthropicClient()

    try:
        run(config, llm, tools)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
