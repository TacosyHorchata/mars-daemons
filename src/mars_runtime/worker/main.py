"""Worker entry point. Spawned by the broker via
`python -m mars_runtime.worker`; reads config + session state from argv,
sets up RPC plumbing + the BrokerLLMClient proxy, and runs the agent
loop. Exits cleanly on KeyboardInterrupt / git / OSError.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess as _sp
import sys
import threading
from pathlib import Path

from ..runtime.agent_loop import run
from ..providers import Message
from ..schema import AgentConfig
from ..tools import ToolRegistry, load_all
from .broker_client import _BrokerLLMClient
from .input import _user_input_stream
from .rpc import _install_event_forwarder, _RPCWriter, _stdin_reader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mars_runtime.worker")
    parser.add_argument("--agent-json", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start-messages-file", default=None)
    args = parser.parse_args(argv)

    config = AgentConfig(**json.loads(args.agent_json))
    data_dir = Path(args.data_dir)
    workspace_path = data_dir / "workspace"
    sessions_dir = data_dir / "sessions"

    start_messages: list[Message] | None = None
    if args.start_messages_file:
        start_messages_path = Path(args.start_messages_file)
        start_messages = json.loads(start_messages_path.read_text(encoding="utf-8"))
        # Temp file is a duplicate of sessions/<id>.json; delete once consumed.
        try:
            start_messages_path.unlink()
        except OSError:
            pass

    writer = _RPCWriter()
    _install_event_forwarder(writer)

    broker_client = _BrokerLLMClient(writer)
    user_queue: queue.Queue = queue.Queue()
    shutdown = threading.Event()

    reader = threading.Thread(
        target=_stdin_reader,
        args=(broker_client, user_queue, shutdown),
        daemon=True,
    )
    reader.start()

    os.chdir(workspace_path)
    load_all()
    tools = ToolRegistry(config.tools or None)

    try:
        run(
            config,
            broker_client,  # type: ignore[arg-type]  # satisfies LLMClient Protocol
            tools,
            turn_source=_user_input_stream(user_queue),
            sessions_dir=sessions_dir,
            session_id=args.session_id,
            workspace_path=workspace_path,
            start_messages=start_messages,
        )
    except KeyboardInterrupt:
        return 130
    except _sp.CalledProcessError as e:
        # Git exited non-zero — surface concisely instead of a traceback.
        print(f"git error: exit {e.returncode} running {e.cmd}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"persistence error: {e}", file=sys.stderr)
        return 1

    return 0
