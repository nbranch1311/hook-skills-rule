#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK = Path(__file__).with_name("livegate.py")


def run_hook(payload: dict[str, object]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp).resolve()
        (workspace / ".git").mkdir()
        (workspace / ".gitignore").write_text("existing-entry\n")
        (workspace / "livegate.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "applications": [
                        {
                            "id": "docs",
                            "name": "Documentation",
                            "commands": ["pnpm dev", "pnpm run docs"],
                        },
                        {
                            "id": "filtered-docs",
                            "packageScripts": ["@scope/docs:dev"],
                        },
                        {
                            "id": "race",
                            "commands": ["pnpm race"],
                        },
                        {
                            "id": "raw-socket",
                            "commands": ["pnpm raw"],
                        },
                    ],
                }
            )
        )
        state_path = workspace / ".livegate" / "servers.json"
        state_path.parent.mkdir()
        state_path.write_text(json.dumps({"version": 1, "servers": {"legacy": {}}}))

        before = lambda command: run_hook(
            {
                "hook_event_name": "beforeShellExecution",
                "command": command,
                "workspace_roots": [str(workspace)],
            }
        )
        post = lambda command, output, exit_code=0: run_hook(
            {
                "hook_event_name": "postToolUse",
                "tool_input": {"command": command},
                "tool_output": {
                    "exitCode": exit_code,
                    "stdout": output,
                },
                "workspace_roots": [str(workspace)],
            }
        )

        assert before("pnpm build") == {"permission": "allow"}
        race_results: list[dict[str, object]] = []
        racers = [
            threading.Thread(target=lambda: race_results.append(before("pnpm race")))
            for _ in range(2)
        ]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join()
        assert sorted(result["permission"] for result in race_results) == ["allow", "deny"]
        state = json.loads(state_path.read_text())
        state["applications"]["race"]["reservedUntil"] = 0
        state_path.write_text(json.dumps(state))
        assert before("pnpm race") == {"permission": "allow"}

        assert before("API_TOKEN=secret pnpm dev") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert state["version"] == 2
        assert state["applications"]["docs"]["state"] == "starting"
        assert "secret" not in state_path.read_text()
        assert "/.livegate/" in (workspace / ".gitignore").read_text().splitlines()

        duplicate = before("pnpm run docs")
        assert duplicate["permission"] == "deny"
        assert "already starting" in duplicate["user_message"]

        server = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            assert post("pnpm dev", f"Local: {url}") == {}
            state = json.loads(state_path.read_text())
            assert state["applications"]["docs"]["state"] == "live"
            assert state["applications"]["docs"]["url"] == url
            duplicate = before("pnpm run docs")
            assert duplicate["permission"] == "deny"
            assert url in duplicate["user_message"]
        finally:
            server.shutdown()
            thread.join()

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        try:
            assert before("pnpm raw") == {"permission": "allow"}
            raw_url = f"http://127.0.0.1:{listener.getsockname()[1]}/"
            assert post("pnpm raw", f"Local: {raw_url}") == {}
            state = json.loads(state_path.read_text())
            assert state["applications"]["raw-socket"]["state"] == "starting"
        finally:
            listener.close()

        assert before("pnpm --filter @scope/docs run dev") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert state["applications"]["filtered-docs"]["state"] == "starting"
        assert post("pnpm --filter @scope/docs run dev", "", exit_code=1) == {}
        state = json.loads(state_path.read_text())
        assert "filtered-docs" not in state["applications"]
        assert state["attempts"][-1]["state"] == "failed"

        state["attempts"] = [
            {"id": str(index), "state": "failed"} for index in range(50)
        ]
        state_path.write_text(json.dumps(state))
        assert before("pnpm --filter @scope/docs run dev") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert len(state["attempts"]) == 50
        assert state["attempts"][0]["id"] == "1"

    print("LiveGate self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
