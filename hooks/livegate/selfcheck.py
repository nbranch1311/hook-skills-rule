#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from livegate import (
    begin_probe,
    fingerprint,
    finish_probe,
    handle_before_shell,
    handle_post_tool,
    is_start_command,
    learned_start,
    load_learned,
    load_state,
    port_open,
    seed_start,
    set_live,
)

WEIRD_COMMAND = "pnpm weird-serve"


def listen() -> socket.socket:
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("::1", 0))
    listener.listen()
    return listener


def main() -> int:
    assert seed_start("pnpm dev")
    assert seed_start("pnpm --filter docs storybook:app")
    assert not seed_start("pnpm build")
    assert not seed_start("pnpm build-storybook")

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp).resolve()
        (workspace / ".git").mkdir()
        (workspace / ".gitignore").write_text("existing-entry\n")

        token = begin_probe(workspace, "pnpm --port 65530 dev", set(), 65530)
        assert finish_probe(workspace, "pnpm --port 65530 dev", 65530, token, "failed", "test failure")
        gitignore = (workspace / ".gitignore").read_bytes()
        assert b"/.livegate/" in gitignore
        assert not (workspace / ".livegate" / "events.log").exists()
        assert load_state(workspace)["servers"]["65530"]["state"] == "failed"

        weird = listen()
        dev = listen()
        storybook = listen()
        try:
            weird_port = weird.getsockname()[1]
            dev_port = dev.getsockname()[1]
            storybook_port = storybook.getsockname()[1]

            set_live(workspace, WEIRD_COMMAND, weird_port, pid=os.getpid())
            learned = load_learned(workspace)
            assert learned_start(learned, fingerprint(WEIRD_COMMAND))
            assert is_start_command(WEIRD_COMMAND, learned)

            denied = handle_before_shell(
                {
                    "hook_event_name": "beforeShellExecution",
                    "command": WEIRD_COMMAND,
                    "workspace_roots": [str(workspace)],
                }
            )
            assert denied["permission"] == "deny"
            assert "Local:" in denied["user_message"]
            assert f"http://localhost:{weird_port}" in denied["user_message"]
            assert f"kill {os.getpid()}" in denied["user_message"]

            set_live(workspace, f"pnpm dev --port {dev_port}", dev_port, pid=os.getpid())
            set_live(workspace, f"pnpm storybook --port {storybook_port}", storybook_port, pid=os.getpid())
            denied_dev = handle_before_shell(
                {
                    "hook_event_name": "beforeShellExecution",
                    "command": f"pnpm dev --port {dev_port}",
                    "workspace_roots": [str(workspace)],
                }
            )
            assert denied_dev["permission"] == "deny"
            assert f"http://localhost:{dev_port}" in denied_dev["user_message"]
            assert f"http://localhost:{storybook_port}" not in denied_dev["user_message"]

            assert (
                handle_before_shell(
                    {
                        "hook_event_name": "beforeShellExecution",
                        "command": f"{os.environ.get('LIVEGATE_RESTART', 'LIVEGATE_RESTART=1')} pnpm dev --port {dev_port}",
                        "workspace_roots": [str(workspace)],
                    }
                )["permission"]
                == "allow"
            )

            handle_post_tool(
                {
                    "hook_event_name": "postToolUse",
                    "tool_input": {"command": f"kill-port {storybook_port}"},
                    "tool_output": '{"exitCode":0}',
                    "workspace_roots": [str(workspace)],
                }
            )
            assert str(storybook_port) not in load_state(workspace)["servers"]
            assert load_learned(workspace)["stops"]

            wrapper = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("livegate.py").resolve()),
                    "--run",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                    "--port",
                    str(weird_port),
                ],
                cwd=workspace,
                check=False,
                capture_output=True,
                text=True,
            )
            assert wrapper.returncode == 1
            assert "Another server is already running." in wrapper.stderr

            weird.close()
            assert (
                handle_before_shell(
                    {
                        "hook_event_name": "beforeShellExecution",
                        "command": WEIRD_COMMAND,
                        "workspace_roots": [str(workspace)],
                    }
                )["permission"]
                == "allow"
            )
        finally:
            for listener in (weird, dev, storybook):
                try:
                    listener.close()
                except OSError:
                    pass

    listener = listen()
    try:
        listener.listen()
        assert port_open(listener.getsockname()[1])
    finally:
        listener.close()

    print("LiveGate self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
