#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from livegate import parse_lsof, parse_proc_started, parse_ss

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
                    "startupTimeoutSeconds": 1,
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
                        {
                            "id": "preexisting",
                            "commands": ["pnpm preexisting"],
                        },
                        {
                            "id": "fallback",
                            "commands": ["pnpm fallback"],
                        },
                        {
                            "id": "timeout",
                            "commands": ["pnpm timeout"],
                        },
                    ],
                }
            )
        )
        state_path = workspace / ".livegate" / "servers.json"
        state_path.parent.mkdir()
        state_path.write_text(json.dumps({"version": 1, "servers": {"legacy": {}}}))

        def before(command: str, session: str = "session-1") -> dict[str, object]:
            return run_hook(
                {
                    "hook_event_name": "beforeShellExecution",
                    "command": command,
                    "session_id": session,
                    "workspace_roots": [str(workspace)],
                }
            )

        def post(
            command: str,
            output: str,
            exit_code: int = 0,
            pid: int | None = None,
            error: str = "",
        ) -> dict[str, object]:
            return run_hook(
                {
                    "hook_event_name": "postToolUse",
                    "tool_input": {"command": command},
                    "tool_output": {
                        "exitCode": exit_code,
                        "pid": pid,
                        "stderr": error,
                        "stdout": output,
                    },
                    "workspace_roots": [str(workspace)],
                }
            )

        assert parse_lsof("p41\nn127.0.0.1:5173\np42\nn[::1]:6006\n") == {
            5173: 41,
            6006: 42,
        }
        assert parse_ss(
            'LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("python",pid=43,fd=3))\n'
        ) == {8000: 43}
        assert parse_proc_started(
            "43 (python worker) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 4242"
        ) == "4242"

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
            assert post("pnpm dev", f"Local: {url}", exit_code=1, pid=os.getpid()) == {}
            state = json.loads(state_path.read_text())
            assert state["applications"]["docs"]["state"] == "live"
            assert state["applications"]["docs"]["url"] == url
            assert state["applications"]["docs"]["processFingerprint"]
            docs_attempt = next(
                attempt
                for attempt in state["attempts"]
                if attempt.get("applicationId") == "docs"
                and attempt.get("state") == "live"
            )
            assert docs_attempt["shell"]["exitCode"] == 1

            rediscovered = before("pnpm build", session="session-2")
            assert rediscovered["permission"] == "allow"
            assert url in rediscovered["agent_message"]
            assert before("pnpm build", session="session-2") == {"permission": "allow"}

            duplicate = before("pnpm run docs")
            assert duplicate["permission"] == "deny"
            assert url in duplicate["user_message"]

            state = json.loads(state_path.read_text())
            state["applications"]["docs"]["processFingerprint"] = "reused"
            state_path.write_text(json.dumps(state))
            assert before("pnpm build", session="session-3") == {"permission": "allow"}
            assert "docs" not in json.loads(state_path.read_text())["applications"]
        finally:
            server.shutdown()
            thread.join()

        preexisting = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        thread = threading.Thread(target=preexisting.serve_forever)
        thread.start()
        try:
            assert before("pnpm preexisting") == {"permission": "allow"}
            existing_url = f"http://127.0.0.1:{preexisting.server_port}/"
            assert post(
                "pnpm preexisting",
                f"Local: {existing_url}",
                pid=os.getpid(),
            ) == {}
            state = json.loads(state_path.read_text())
            assert state["applications"]["preexisting"]["state"] == "starting"
        finally:
            preexisting.shutdown()
            thread.join()

        assert before("pnpm fallback") == {"permission": "allow"}
        fallback = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        thread = threading.Thread(target=fallback.serve_forever)
        thread.start()
        try:
            assert post("pnpm fallback", "ready", pid=os.getpid()) == {}
            state = json.loads(state_path.read_text())
            assert state["applications"]["fallback"]["state"] == "live"
            assert state["applications"]["fallback"]["url"].endswith(
                f":{fallback.server_port}/"
            )
        finally:
            fallback.shutdown()
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

        assert before("pnpm timeout") == {"permission": "allow"}
        assert post("pnpm timeout", "still building", pid=os.getpid()) == {}
        time.sleep(1.5)
        state = json.loads(state_path.read_text())
        assert "timeout" not in state["applications"]
        timeout_attempt = next(
            attempt
            for attempt in reversed(state["attempts"])
            if attempt.get("applicationId") == "timeout"
        )
        assert timeout_attempt["state"] == "failed"
        assert "within 1 seconds" in timeout_attempt["reason"]

        assert before("pnpm --filter @scope/docs run dev") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert state["applications"]["filtered-docs"]["state"] == "starting"
        assert post(
            "pnpm --filter @scope/docs run dev",
            "",
            exit_code=1,
            error="API_TOKEN=secret DATABASE_PASSWORD=hunter2 crash",
        ) == {}
        time.sleep(1.5)
        state = json.loads(state_path.read_text())
        assert "filtered-docs" not in state["applications"]
        assert state["attempts"][-1]["state"] == "failed"
        assert (
            state["attempts"][-1]["reason"]
            == "API_TOKEN=<redacted> DATABASE_PASSWORD=<redacted> crash"
        )

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
