#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from livegate import (
    approval_matches,
    command_cwd,
    inferred_application,
    inspection_command,
    listener_pids,
    parse_lsof,
    parse_proc_started,
    parse_ss,
    state_lock,
)

HOOK = Path(__file__).with_name("livegate.py")


def run_hook(
    payload: dict[str, object],
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        env={**os.environ, **(environment or {})},
        text=True,
    )
    return json.loads(result.stdout)


def run_raw(value: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=value,
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
                    "startupTimeoutSeconds": 5,
                    "applications": [
                        {
                            "id": "docs",
                            "name": "Documentation",
                            "commands": ["pnpm dev", "pnpm run docs"],
                        },
                        {
                            "id": "filtered-docs",
                            "packageScripts": ["@scope/mapped:dev"],
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
                        {
                            "id": "stack-ui",
                            "name": "Stack UI",
                            "commands": ["pnpm stack"],
                            "endpointIndex": 0,
                        },
                        {
                            "id": "stack-api",
                            "name": "Stack API",
                            "commands": ["pnpm stack"],
                            "endpointIndex": 1,
                        },
                    ],
                }
            )
        )
        packages = {
            "docs": ("@scope/docs", {"dev": "vite", "storybook": "storybook dev"}),
            "npm": ("@scope/npm", {"dev": "vite"}),
            "yarn": ("@scope/yarn", {"dev": "vite"}),
            "bun": ("@scope/bun", {"dev": "vite"}),
            "direct": ("@scope/direct", {}),
            "mapped": ("@scope/mapped", {"dev": "vite"}),
        }
        for directory, (name, scripts) in packages.items():
            package = workspace / "apps" / directory
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"name": name, "scripts": scripts})
            )
        (workspace / "package.json").write_text(
            json.dumps(
                {
                    "name": "root",
                    "scripts": {"docs-alias": "pnpm --filter @scope/docs dev"},
                }
            )
        )
        state_path = workspace / ".livegate" / "servers.json"
        state_path.parent.mkdir()
        state_path.write_text(json.dumps({"version": 1, "servers": {"legacy": {}}}))

        def before(
            command: str,
            session: str = "session-1",
            cwd: Path = workspace,
            roots: list[Path] | None = None,
        ) -> dict[str, object]:
            return run_hook(
                {
                    "hook_event_name": "beforeShellExecution",
                    "command": command,
                    "cwd": str(cwd),
                    "session_id": session,
                    "workspace_roots": [str(root) for root in (roots or [workspace])],
                }
            )

        def post(
            command: str,
            output: str,
            exit_code: int = 0,
            pid: int | None = None,
            error: str = "",
            cwd: Path = workspace,
            roots: list[Path] | None = None,
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
                    "cwd": str(cwd),
                    "workspace_roots": [str(root) for root in (roots or [workspace])],
                }
            )

        assert inspection_command("Windows") is None
        real_run = subprocess.run

        def time_out_inspection(
            command: list[str],
            *args: object,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if command[0] in {"lsof", "ss"}:
                raise subprocess.TimeoutExpired(command, 1)
            return real_run(command, *args, **kwargs)

        with patch(
            "livegate.subprocess.run",
            side_effect=time_out_inspection,
        ):
            try:
                listener_pids()
                raise AssertionError("inspection timeout did not fail open")
            except RuntimeError:
                pass
        malformed = run_raw("{")
        assert malformed["permission"] == "allow"
        assert "warning" in malformed["agent_message"]

        missing_tool = run_hook(
            {
                "hook_event_name": "beforeShellExecution",
                "command": "pnpm dev",
                "cwd": str(workspace),
                "workspace_roots": [str(workspace)],
            },
            environment={"PATH": ""},
        )
        assert missing_tool["permission"] == "allow"
        assert "warning" in missing_tool["agent_message"]

        with state_lock(workspace):
            contended = before("pnpm dev")
        assert contended["permission"] == "allow"
        assert "warning" in contended["agent_message"]

        state_path.write_text("{")
        corrupt = before("pnpm build")
        assert corrupt["permission"] == "allow"
        assert "warning" in corrupt["agent_message"]
        state_path.write_text(json.dumps({"version": 1}))

        started = time.perf_counter()
        assert before("echo performance") == {"permission": "allow"}
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2, elapsed

        assert before("./privacy-check") == {"permission": "allow"}
        assert post(
            "./privacy-check",
            "API_TOKEN=raw-secret FULL_OUTPUT_MARKER",
        ) == {}
        persisted = state_path.read_text()
        assert "raw-secret" not in persisted
        assert "FULL_OUTPUT_MARKER" not in persisted

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
        assert command_cwd(
            {
                "cwd": str(workspace),
                "tool_input": {"working_directory": str(workspace / "apps" / "docs")},
            }
        ) == workspace / "apps" / "docs"
        approval = {
            "applicationId": "docs",
            "commandHash": "command",
            "expiresAt": time.time() + 300,
            "session": "session",
            "workspace": str(workspace),
        }
        assert approval_matches(approval, workspace, "docs", "command", "session")
        assert not approval_matches(
            approval,
            workspace / "other",
            "docs",
            "command",
            "session",
        )
        assert not approval_matches(approval, workspace, "other", "command", "session")
        assert not approval_matches(approval, workspace, "docs", "other", "session")
        assert not approval_matches(approval, workspace, "docs", "command", "other")
        assert not approval_matches(
            {**approval, "expiresAt": 0},
            workspace,
            "docs",
            "command",
            "session",
        )

        assert inferred_application(workspace, workspace, "pnpm docs-alias") == {
            "id": "apps/docs:vite",
            "name": "@scope/docs vite",
        }
        assert inferred_application(
            workspace,
            workspace,
            "pnpm --filter @scope/docs dev",
        ) == {
            "id": "apps/docs:vite",
            "name": "@scope/docs vite",
        }
        assert before("pnpm docs-alias") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert state["applications"]["apps/docs:vite"]["state"] == "starting"
        assert state["applications"]["apps/docs:vite"]["reservedUntil"] > time.time()
        filtered_docs = before("pnpm --filter @scope/docs dev")
        assert filtered_docs["permission"] == "deny", filtered_docs
        assert before("pnpm --filter @scope/docs storybook") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert state["applications"]["apps/docs:storybook"]["state"] == "starting"

        assert before("npm run dev --workspace @scope/npm") == {"permission": "allow"}
        assert before("yarn workspace @scope/yarn dev") == {"permission": "allow"}
        assert before("yarn workspace @scope/yarn run dev")["permission"] == "deny"
        assert before(
            "bun run dev",
            cwd=workspace / "apps" / "bun",
        ) == {"permission": "allow"}
        assert before(
            "pnpm exec vite",
            cwd=workspace / "apps" / "direct",
        ) == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert {
            "apps/npm:vite",
            "apps/yarn:vite",
            "apps/bun:vite",
            "apps/direct:vite",
        }.issubset(state["applications"])
        assert inferred_application(
            workspace,
            workspace,
            "pnpm exec vite --config apps/direct/vite.config.ts",
        )["id"] == "apps/direct:vite"
        assert inferred_application(
            workspace,
            workspace,
            "storybook dev -c apps/docs/.storybook",
        )["id"] == "apps/docs:storybook"
        assert inferred_application(
            workspace,
            workspace,
            "storybook dev --config-dir apps/docs/.storybook",
        )["id"] == "apps/docs:storybook"
        assert before("pnpm exec vite build") == {"permission": "allow"}
        assert before("storybook build") == {"permission": "allow"}

        duplicate = workspace / "duplicates" / "npm"
        duplicate.mkdir(parents=True)
        (duplicate / "package.json").write_text(
            json.dumps({"name": "@scope/npm", "scripts": {"dev": "vite"}})
        )
        root_package = json.loads((workspace / "package.json").read_text())
        root_package["scripts"]["dev"] = "vite"
        (workspace / "package.json").write_text(json.dumps(root_package))
        assert (
            inferred_application(
                workspace,
                workspace,
                "npm run dev --workspace @scope/npm",
            )
            is None
        )
        assert before("npm run dev --workspace @scope/npm") == {"permission": "allow"}

        secondary = workspace / "secondary"
        secondary.mkdir()
        (secondary / "package.json").write_text(
            json.dumps({"name": "secondary", "scripts": {"dev": "vite"}})
        )
        assert before(
            "pnpm dev",
            cwd=secondary,
            roots=[workspace, secondary],
        ) == {"permission": "allow"}
        secondary_state = json.loads(
            (secondary / ".livegate" / "servers.json").read_text()
        )
        assert secondary_state["applications"][".:vite"]["state"] == "starting"

        assert before("./opaque-server") == {"permission": "allow"}
        assert post("./opaque-server", "completed") == {}
        assert before("./opaque-server") == {"permission": "allow"}

        assert before("pnpm stack") == {"permission": "allow"}
        stack_ui = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        stack_api = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        stack_ui_thread = threading.Thread(target=stack_ui.serve_forever)
        stack_api_thread = threading.Thread(target=stack_api.serve_forever)
        stack_ui_thread.start()
        try:
            stack_ui_url = f"http://127.0.0.1:{stack_ui.server_port}/"
            stack_api_url = f"http://127.0.0.1:{stack_api.server_port}/"
            assert post(
                "pnpm stack",
                f"UI: {stack_ui_url}\nAPI: {stack_api_url}",
                pid=os.getpid(),
            ) == {}
            stack_api_thread.start()
            time.sleep(2)
            state = json.loads(state_path.read_text())
            configured_group_id = next(
                group_id
                for group_id, group in state["groups"].items()
                if group.get("applicationIds") == ["stack-ui", "stack-api"]
            )
            assert state["groups"][configured_group_id]["state"] == "live"
            assert {
                member["applicationId"]
                for member in state["groups"][configured_group_id]["members"]
            } == {"stack-ui", "stack-api"}
            group_duplicate = before("pnpm stack")
            assert group_duplicate["permission"] == "deny"
            assert stack_ui_url in group_duplicate["user_message"]
            assert stack_api_url in group_duplicate["user_message"]
            assert "targeted recovery, full restart, or launch a second group" in group_duplicate["user_message"]

            stack_ui.shutdown()
            stack_ui_thread.join()
            assert before("pnpm build", session="group-session")["permission"] == "allow"
            state = json.loads(state_path.read_text())
            assert state["groups"][configured_group_id]["state"] == "degraded"
            degraded = before("pnpm stack")
            assert degraded["permission"] == "deny"
            assert "Stack UI" in degraded["user_message"]
            assert "Stack API" in degraded["user_message"]
            assert stack_api_url in degraded["user_message"]

            token = re.search(
                r"LIVEGATE_SECOND=([a-f0-9]+)",
                degraded["agent_message"],
            ).group(1)
            approved_group = f"LIVEGATE_SECOND={token} pnpm stack"
            assert before(approved_group) == {"permission": "allow"}
            second_ui = HTTPServer(("127.0.0.1", 0), HealthyHandler)
            second_api = HTTPServer(("127.0.0.1", 0), HealthyHandler)
            second_ui_thread = threading.Thread(target=second_ui.serve_forever)
            second_api_thread = threading.Thread(target=second_api.serve_forever)
            second_ui_thread.start()
            second_api_thread.start()
            try:
                second_ui_url = f"http://127.0.0.1:{second_ui.server_port}/"
                second_api_url = f"http://127.0.0.1:{second_api.server_port}/"
                assert post(
                    approved_group,
                    f"UI: {second_ui_url}\nAPI: {second_api_url}",
                    pid=os.getpid(),
                ) == {}
                state = json.loads(state_path.read_text())
                assert {
                    member["url"]
                    for member in state["groups"][configured_group_id]["members"]
                } == {stack_api_url, second_ui_url, second_api_url}
                assert len(
                    {
                        member["attemptId"]
                        for member in state["groups"][configured_group_id]["members"]
                    }
                ) == 2
            finally:
                second_ui.shutdown()
                second_api.shutdown()
                second_ui_thread.join()
                second_api_thread.join()
        finally:
            if stack_ui_thread.is_alive():
                stack_ui.shutdown()
                stack_ui_thread.join()
            if stack_api_thread.is_alive():
                stack_api.shutdown()
                stack_api_thread.join()

        assert before("./multi-server") == {"permission": "allow"}
        multi_a = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        multi_b = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        multi_a_thread = threading.Thread(target=multi_a.serve_forever)
        multi_b_thread = threading.Thread(target=multi_b.serve_forever)
        multi_a_thread.start()
        multi_b_thread.start()
        try:
            multi_a_url = f"http://127.0.0.1:{multi_a.server_port}/"
            multi_b_url = f"http://127.0.0.1:{multi_b.server_port}/"
            assert post(
                "./multi-server",
                f"{multi_a_url}\n{multi_b_url}",
                pid=os.getpid(),
            ) == {}
            state = json.loads(state_path.read_text())
            assert state["learnedGroups"]
            fallback_group_id = next(iter(state["learnedGroups"].values()))["id"]
            assert state["groups"][fallback_group_id]["expectedMembers"] == 2
            assert before("./multi-server")["permission"] == "deny"
        finally:
            multi_a.shutdown()
            multi_b.shutdown()
            multi_a_thread.join()
            multi_b_thread.join()

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
        assert state["version"] == 3
        assert state["applications"]["docs"]["state"] == "starting"
        assert "secret" not in state_path.read_text()
        assert "/.livegate/" in (workspace / ".gitignore").read_text().splitlines()

        duplicate = before("pnpm run docs")
        assert duplicate["permission"] == "deny"
        assert "already starting" in duplicate["user_message"]

        server = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        first_thread = threading.Thread(target=server.serve_forever)
        first_thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            assert post("pnpm dev", f"Local: {url}", exit_code=1, pid=os.getpid()) == {}
            state = json.loads(state_path.read_text())
            assert state["applications"]["docs"]["state"] == "live"
            assert state["applications"]["docs"]["instances"][0]["url"] == url
            assert state["applications"]["docs"]["instances"][0]["processFingerprint"]
            docs_attempt = next(
                attempt
                for attempt in state["attempts"]
                if attempt.get("applicationId") == "docs"
                and attempt.get("state") == "live"
            )
            assert docs_attempt["shell"]["exitCode"] == 1

            started = time.perf_counter()
            rediscovered = before("pnpm build", session="session-2")
            assert time.perf_counter() - started < 0.2
            assert rediscovered["permission"] == "allow"
            assert url in rediscovered["agent_message"]
            assert before("pnpm build", session="session-2") == {"permission": "allow"}

            duplicate = before("pnpm run docs")
            assert duplicate["permission"] == "deny"
            assert url in duplicate["user_message"]
            assert "reuse, restart, or launch a second instance" in duplicate["user_message"]
            assert str(os.getpid()) in duplicate["agent_message"]
            token = re.search(
                r"LIVEGATE_SECOND=([a-f0-9]+)",
                duplicate["agent_message"],
            ).group(1)

            assert before("LIVEGATE_SECOND=wrong pnpm run docs")["permission"] == "deny"
            approved = f"LIVEGATE_SECOND={token} pnpm run docs"
            assert before(approved) == {"permission": "allow"}

            second = HTTPServer(("127.0.0.1", 0), HealthyHandler)
            second_thread = threading.Thread(target=second.serve_forever)
            second_thread.start()
            try:
                second_url = f"http://127.0.0.1:{second.server_port}/"
                assert post(
                    approved,
                    f"Local: {second_url}",
                    pid=os.getpid(),
                ) == {}
                state = json.loads(state_path.read_text())
                assert [instance["url"] for instance in state["applications"]["docs"]["instances"]] == [
                    url,
                    second_url,
                ]
                assert before(approved)["permission"] == "deny"

                duplicate = before("pnpm run docs")
                assert url in duplicate["user_message"]
                assert second_url in duplicate["user_message"]
                stale_token = re.search(
                    r"LIVEGATE_SECOND=([a-f0-9]+)",
                    duplicate["agent_message"],
                ).group(1)
                state = json.loads(state_path.read_text())
                state["approvals"][stale_token]["expiresAt"] = 0
                state_path.write_text(json.dumps(state))
                assert before(
                    f"LIVEGATE_SECOND={stale_token} pnpm run docs"
                )["permission"] == "deny"

                server.shutdown()
                first_thread.join()
                assert before("pnpm build", session="session-3")["permission"] == "allow"
                state = json.loads(state_path.read_text())
                assert [instance["url"] for instance in state["applications"]["docs"]["instances"]] == [
                    second_url
                ]

                second.shutdown()
                second_thread.join()
                assert before("pnpm build", session="session-4") == {"permission": "allow"}
                assert "docs" not in json.loads(state_path.read_text())["applications"]
                assert before("pnpm run docs") == {"permission": "allow"}
            finally:
                if second_thread.is_alive():
                    second.shutdown()
                    second_thread.join()
        finally:
            if first_thread.is_alive():
                server.shutdown()
                first_thread.join()

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
            assert state["applications"]["fallback"]["instances"][0]["url"].endswith(
                f":{fallback.server_port}/"
            )
        finally:
            fallback.shutdown()
            thread.join()

        assert before("./serve-local") == {"permission": "allow"}
        learned = HTTPServer(("127.0.0.1", 0), HealthyHandler)
        thread = threading.Thread(target=learned.serve_forever)
        thread.start()
        try:
            learned_url = f"http://127.0.0.1:{learned.server_port}/"
            assert post(
                "./serve-local",
                f"Local: {learned_url}",
                pid=os.getpid(),
            ) == {}
            assert before("./serve-local")["permission"] == "deny"
            state = json.loads(state_path.read_text())
            assert state["learned"]
        finally:
            learned.shutdown()
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

        config = json.loads((workspace / "livegate.json").read_text())
        config["startupTimeoutSeconds"] = 1
        (workspace / "livegate.json").write_text(json.dumps(config))
        assert before("pnpm timeout") == {"permission": "allow"}
        assert post("pnpm timeout", "still building", pid=os.getpid()) == {}
        time.sleep(2.5)
        state = json.loads(state_path.read_text())
        assert "timeout" not in state["applications"]
        timeout_attempt = next(
            attempt
            for attempt in reversed(state["attempts"])
            if attempt.get("applicationId") == "timeout"
        )
        assert timeout_attempt["state"] == "failed"
        assert "within 1 seconds" in timeout_attempt["reason"]
        timeout_notice = before("pnpm --filter @scope/mapped run dev")
        assert timeout_notice["permission"] == "allow"
        assert "timeout failed" in timeout_notice["agent_message"]
        state = json.loads(state_path.read_text())
        assert state["applications"]["filtered-docs"]["state"] == "starting"
        assert post(
            "pnpm --filter @scope/mapped run dev",
            "",
            exit_code=1,
            error="API_TOKEN=secret DATABASE_PASSWORD=hunter2 ACCOUNT=private crash",
        ) == {}
        time.sleep(2.5)
        state = json.loads(state_path.read_text())
        assert "filtered-docs" not in state["applications"]
        assert state["attempts"][-1]["state"] == "failed"
        assert (
            state["attempts"][-1]["reason"]
            == "API_TOKEN=<redacted> DATABASE_PASSWORD=<redacted> ACCOUNT=<redacted> crash"
        )
        failure_notice = before("pnpm build", session="failure-session")
        assert "ACCOUNT=<redacted>" in failure_notice["agent_message"]
        assert "private" not in failure_notice["agent_message"]
        assert before("pnpm build", session="failure-session") == {"permission": "allow"}

        state["attempts"] = [
            {"id": str(index), "notified": True, "state": "failed"}
            for index in range(50)
        ]
        state_path.write_text(json.dumps(state))
        assert before("pnpm --filter @scope/mapped run dev") == {"permission": "allow"}
        state = json.loads(state_path.read_text())
        assert len(state["attempts"]) == 50
        assert state["attempts"][0]["id"] == "1"

    print("LiveGate self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
