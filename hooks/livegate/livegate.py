#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "recipes.json"
STATE_NAME = "servers.json"
IGNORE_ENTRY = "/.livegate/"
TOKEN_ROOT = Path(tempfile.gettempdir()) / "livegate"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workspace_root(payload: dict[str, Any]) -> Path:
    roots = payload.get("workspace_roots") or []
    return Path(roots[0] if roots else payload.get("cwd") or ".").expanduser().resolve()


def state_path(workspace: Path) -> Path:
    return workspace / ".livegate" / STATE_NAME


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def load_state(workspace: Path) -> dict[str, Any]:
    path = state_path(workspace)
    if not path.exists():
        return {"version": 1, "servers": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"version": 1, "servers": {}}
    data.setdefault("version", 1)
    data.setdefault("servers", {})
    return data


def ensure_gitignore(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        return
    path = workspace / ".gitignore"
    content = path.read_bytes() if path.exists() else b""
    if IGNORE_ENTRY in content.decode(errors="ignore").splitlines():
        return
    with path.open("ab") as handle:
        handle.write((b"" if not content or content.endswith((b"\n", b"\r")) else b"\n") + f"{IGNORE_ENTRY}\n".encode())


@contextmanager
def state_lock(workspace: Path):
    directory = workspace / ".livegate"
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".lock"
    deadline = time.monotonic() + 5
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                if time.time() - lock.stat().st_mtime > 5:
                    lock.rmdir()
                    continue
                raise TimeoutError("LiveGate state is busy")
            time.sleep(0.02)
    try:
        yield
    finally:
        lock.rmdir()


def save_state(workspace: Path, data: dict[str, Any]) -> None:
    path = state_path(workspace)
    if not path.exists():
        ensure_gitignore(workspace)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def update_state(workspace: Path, update: Any) -> dict[str, Any]:
    with state_lock(workspace):
        data = load_state(workspace)
        update(data["servers"])
        save_state(workspace, data)
        return data


def port_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_for_port(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return next((int(line) for line in result.stdout.splitlines() if line.isdigit()), None)


def parse_port(command: str, default: int) -> int:
    match = re.search(r"(?:--port|-p)\s+([0-9]{2,5})\b", command)
    return int(match.group(1)) if match else default


def matches(command: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, command, re.IGNORECASE) for pattern in patterns)


def start_recipe(command: str, config: dict[str, Any]) -> dict[str, Any] | None:
    for recipe in config["recipes"]:
        if matches(command, recipe.get("exclude", [])):
            continue
        if matches(command, recipe["start"]):
            return recipe
    return None


def is_stop_command(command: str, config: dict[str, Any]) -> bool:
    return matches(command, config["stop"]) or any(
        matches(command, recipe.get("stop", [])) for recipe in config["recipes"]
    )


def prune_closed(workspace: Path) -> None:
    data = load_state(workspace)
    closed = [
        server_id
        for server_id, server in data["servers"].items()
        if server["state"] == "live" and not port_open(int(server["port"]))
    ]
    if closed:
        update_state(workspace, lambda servers: [servers.pop(server_id, None) for server_id in closed])


def set_live(workspace: Path, recipe: dict[str, Any], port: int) -> None:
    update_state(
        workspace,
        lambda servers: servers.__setitem__(
            recipe["id"],
            {
                "state": "live",
                "name": recipe.get("name", recipe["id"]),
                "port": port,
                "url": f"http://localhost:{port}",
                "pid": pid_for_port(port),
                "since": now(),
            },
        ),
    )


def remove_server(workspace: Path, server_id: str) -> None:
    if server_id in load_state(workspace)["servers"]:
        update_state(workspace, lambda servers: servers.pop(server_id, None))


def token_prefix(workspace: Path, recipe_id: str, port: int | None = None) -> str:
    workspace_id = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    return f"{workspace_id}-{recipe_id}-{f'{port}-' if port is not None else ''}"


def begin_probe(workspace: Path, recipe_id: str, port: int) -> str:
    TOKEN_ROOT.mkdir(parents=True, exist_ok=True)
    cancel_probe(workspace, recipe_id)
    token = secrets.token_hex(8)
    (TOKEN_ROOT / f"{token_prefix(workspace, recipe_id, port)}{token}").touch()
    return token


def probe_active(workspace: Path, recipe_id: str, port: int, token: str) -> bool:
    return (TOKEN_ROOT / f"{token_prefix(workspace, recipe_id, port)}{token}").exists()


def cancel_probe(
    workspace: Path,
    recipe_id: str,
    port: int | None = None,
    token: str | None = None,
) -> None:
    pattern = f"{token_prefix(workspace, recipe_id, port)}{token or '*'}"
    for path in TOKEN_ROOT.glob(pattern):
        path.unlink(missing_ok=True)


def finish_probe(
    workspace: Path,
    recipe: dict[str, Any],
    port: int,
    token: str,
    state: str,
    reason: str | None = None,
) -> bool:
    with state_lock(workspace):
        if not probe_active(workspace, recipe["id"], port, token):
            return False
        cancel_probe(workspace, recipe["id"], port, token)
        data = load_state(workspace)
        entry = {
            "state": state,
            "name": recipe.get("name", recipe["id"]),
            "port": port,
        }
        if state == "live":
            entry.update(url=f"http://localhost:{port}", pid=pid_for_port(port), since=now())
        else:
            entry["at"] = now()
            entry["reason"] = (reason or "Failed to start")[:500]
        data["servers"][recipe["id"]] = entry
        save_state(workspace, data)
        return True


def launch_probe(workspace: Path, recipe_id: str, port: int, timeout: int) -> None:
    token = begin_probe(workspace, recipe_id, port)
    subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--probe",
            str(workspace),
            recipe_id,
            str(port),
            str(timeout),
            token,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def probe(workspace: Path, recipe_id: str, port: int, timeout: int, token: str) -> int:
    config = load_config()
    recipe = next(recipe for recipe in config["recipes"] if recipe["id"] == recipe_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not probe_active(workspace, recipe_id, port, token):
            return 0
        if port_open(port):
            finish_probe(workspace, recipe, port, token, "live")
            return 0
        current = load_state(workspace)["servers"].get(recipe_id)
        if current and current["state"] == "failed":
            cancel_probe(workspace, recipe_id, port, token)
            return 1
        time.sleep(0.5)
    if probe_active(workspace, recipe_id, port, token):
        finish_probe(
            workspace,
            recipe,
            port,
            token,
            "failed",
            f"Port {port} did not open within {timeout} seconds",
        )
    return 1


def hook_result(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("tool_output")
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {}
    return output if isinstance(output, dict) else {}


def failure_reason(result: dict[str, Any]) -> str:
    text = result.get("stderr") or result.get("stdout") or result.get("message")
    if text:
        return " ".join(str(text).split())[-500:]
    return f"Shell command exited with code {result.get('exitCode', 'unknown')}"


def handle_stop(workspace: Path, command: str, config: dict[str, Any]) -> None:
    command_ports = {int(value) for value in re.findall(r"(?<!\d)(\d{2,5})(?!\d)", command)}
    with state_lock(workspace):
        data = load_state(workspace)
        for recipe in config["recipes"]:
            for port in command_ports:
                cancel_probe(workspace, recipe["id"], port)
            default_port = int(recipe["port"])
            if matches(command, recipe.get("stop", [])) or default_port in command_ports:
                cancel_probe(workspace, recipe["id"])
                data["servers"].pop(recipe["id"], None)

        for server_id, server in list(data["servers"].items()):
            pid = server.get("pid")
            numbers = {int(server["port"]), *([int(pid)] if pid else [])}
            if numbers & command_ports or re.search(rf"\b{re.escape(server_id)}\b", command):
                cancel_probe(workspace, server_id)
                data["servers"].pop(server_id, None)

        if state_path(workspace).exists():
            save_state(workspace, data)


def handle_before_shell(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command") or ""
    workspace = workspace_root(payload)
    prune_closed(workspace)
    config = load_config()
    if is_stop_command(command, config):
        return {"permission": "allow"}
    recipe = start_recipe(command, config)
    if not recipe:
        return {"permission": "allow"}

    port = parse_port(command, int(recipe["port"]))
    if port_open(port) and "LIVEGATE_RESTART=1" not in command:
        set_live(workspace, recipe, port)
        name = recipe.get("name", recipe["id"])
        return {
            "permission": "deny",
            "user_message": f"The server {name} is running on :{port}. Restart it?",
            "agent_message": f'The server {name} is running. Ask the user: "Do you want me to restart it?"',
        }

    remove_server(workspace, recipe["id"])
    launch_probe(workspace, recipe["id"], port, int(config.get("probeTimeoutSeconds", 60)))
    return {"permission": "allow"}


def handle_post_tool(payload: dict[str, Any]) -> None:
    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = workspace_root(payload)
    result = hook_result(payload)
    config = load_config()
    recipe = start_recipe(command, config)
    if recipe and result.get("exitCode") not in (None, 0):
        port = parse_port(command, int(recipe["port"]))
        if not port_open(port):
            token_files = list(TOKEN_ROOT.glob(f"{token_prefix(workspace, recipe['id'], port)}*"))
            token = token_files[0].name.rsplit("-", 1)[-1] if token_files else None
            if token:
                finish_probe(workspace, recipe, port, token, "failed", failure_reason(result))
            else:
                current = load_state(workspace)["servers"].get(recipe["id"])
                if current and current["state"] == "live":
                    remove_server(workspace, recipe["id"])
        return

    if result.get("exitCode") != 0 or not is_stop_command(command, config):
        return

    handle_stop(workspace, command, config)


def main() -> int:
    if len(sys.argv) == 7 and sys.argv[1] == "--probe":
        return probe(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6])

    payload = json.loads(sys.stdin.read() or "{}")
    if payload.get("hook_event_name") == "beforeShellExecution":
        print(json.dumps(handle_before_shell(payload)))
    elif payload.get("hook_event_name") == "postToolUse":
        handle_post_tool(payload)
        print("{}")
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
