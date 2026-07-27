#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path

from livegate import (
    begin_probe,
    finish_probe,
    handle_before_shell,
    handle_post_tool,
    load_config,
    load_state,
    port_open,
    probe,
    remove_server,
    set_live,
    start_recipe,
)


def main() -> int:
    config = load_config()
    dev = start_recipe("pnpm dev", config)
    storybook = start_recipe("pnpm --filter docs storybook:app", config)
    assert dev and dev["id"] == "dev"
    assert storybook and storybook["id"] == "storybook"
    assert start_recipe("pnpm build", config) is None

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp).resolve()
        (workspace / ".git").mkdir()
        (workspace / ".gitignore").write_text("existing-entry\n")
        token = begin_probe(workspace, "dev", 65530)
        assert finish_probe(workspace, dev, 65530, token, "failed", "test failure")
        gitignore = (workspace / ".gitignore").read_bytes()
        assert b"/.livegate/" in gitignore
        assert not (workspace / ".livegate" / "events.log").exists()
        assert load_state(workspace)["servers"]["dev"]["state"] == "failed"

        set_live(workspace, dev, 65530)
        assert (workspace / ".gitignore").read_bytes() == gitignore
        assert load_state(workspace)["servers"]["dev"]["state"] == "live"
        remove_server(workspace, "dev")
        assert load_state(workspace)["servers"] == {}

        def fail(recipe: dict[str, object], port: int) -> None:
            token = begin_probe(workspace, str(recipe["id"]), port)
            finish_probe(workspace, recipe, port, token, "failed", "failed")

        threads = [
            threading.Thread(target=fail, args=(recipe, 65000 + index))
            for index, recipe in enumerate((dev, storybook))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert set(load_state(workspace)["servers"]) == {"dev", "storybook"}

        handle_post_tool(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": "pkill device-manager"},
                "tool_output": '{"exitCode":0}',
                "workspace_roots": [str(workspace)],
            }
        )
        assert "dev" in load_state(workspace)["servers"]

        assert (
            handle_before_shell(
                {
                    "hook_event_name": "beforeShellExecution",
                    "command": "pnpm storybook:stop",
                    "workspace_roots": [str(workspace)],
                }
            )["permission"]
            == "allow"
        )
        handle_post_tool(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": "pnpm storybook:stop"},
                "tool_output": '{"exitCode":0}',
                "workspace_roots": [str(workspace)],
            }
        )
        assert "storybook" not in load_state(workspace)["servers"]

        handle_post_tool(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": "pnpm dev"},
                "tool_output": '{"exitCode":0}',
                "workspace_roots": [str(workspace)],
            }
        )
        assert "dev" in load_state(workspace)["servers"]

        handle_post_tool(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": "kill-port 15173"},
                "tool_output": '{"exitCode":0}',
                "workspace_roots": [str(workspace)],
            }
        )
        assert "dev" in load_state(workspace)["servers"]

        begin_probe(workspace, "dev", 5173)
        handle_post_tool(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": "pnpm dev"},
                "tool_output": json.dumps({"exitCode": 1, "stderr": "server crashed"}),
                "workspace_roots": [str(workspace)],
            }
        )
        assert load_state(workspace)["servers"]["dev"]["reason"] == "server crashed"

        remove_server(workspace, "dev")
        token = begin_probe(workspace, "dev", 65530)
        handle_post_tool(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": "kill-port 65530"},
                "tool_output": '{"exitCode":0}',
                "workspace_roots": [str(workspace)],
            }
        )
        probe(workspace, "dev", 65530, 0, token)
        assert "dev" not in load_state(workspace)["servers"]

    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        listener.bind(("::1", 0))
        listener.listen()
        assert port_open(listener.getsockname()[1])
    finally:
        listener.close()

    print("LiveGate self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
