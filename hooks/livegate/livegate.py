#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_NAME = "servers.json"
LEARNED_NAME = "learned.json"
IGNORE_ENTRY = "/.livegate/"
TOKEN_ROOT = Path(tempfile.gettempdir()) / "livegate"
PROBE_TIMEOUT_SECONDS = 60
RESTART_ENV = "LIVEGATE_RESTART=1"

# ponytail: seed heuristics only decide when to observe; learned evidence does the gating.
START_RE = re.compile(r"\b(?:dev|serve|start|storybook|vite|next|nuxt|astro|uvicorn|flask|runserver|livegate-run)\b", re.I)
EXCLUDE_RE = re.compile(r"\b(?:test|vitest|build|lint|typecheck|build-storybook|storybook\s+build)\b", re.I)
STOP_RE = re.compile(r"\b(?:kill-port|pkill|kill\s+|taskkill|Stop-Process|lsof\s+-ti)\b", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workspace_root(payload: dict[str, Any]) -> Path:
    roots = payload.get("workspace_roots") or []
    return Path(roots[0] if roots else payload.get("cwd") or ".").expanduser().resolve()


def livegate_dir(workspace: Path) -> Path:
    return workspace / ".livegate"


def state_path(workspace: Path) -> Path:
    return livegate_dir(workspace) / STATE_NAME


def learned_path(workspace: Path) -> Path:
    return livegate_dir(workspace) / LEARNED_NAME


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
    directory = livegate_dir(workspace)
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


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return default.copy()
    return data if isinstance(data, dict) else default.copy()


def write_json(workspace: Path, path: Path, data: dict[str, Any]) -> None:
    if not path.exists():
        ensure_gitignore(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def load_state(workspace: Path) -> dict[str, Any]:
    data = read_json(state_path(workspace), {"version": 1, "servers": {}})
    data.setdefault("version", 1)
    data.setdefault("servers", {})
    return data


def save_state(workspace: Path, data: dict[str, Any]) -> None:
    write_json(workspace, state_path(workspace), data)


def load_learned(workspace: Path) -> dict[str, Any]:
    data = read_json(learned_path(workspace), {"version": 1, "starts": [], "stops": []})
    data.setdefault("version", 1)
    data.setdefault("starts", [])
    data.setdefault("stops", [])
    return data


def save_learned(workspace: Path, data: dict[str, Any]) -> None:
    write_json(workspace, learned_path(workspace), data)


def normalize_command(command: str) -> str:
    command = re.sub(r"^\s*(?:env\s+)?LIVEGATE_RESTART=1\s+", "", command)
    return " ".join(command.strip().split())


def fingerprint(command: str) -> str:
    return normalize_command(command)


def command_hash(command_fingerprint: str) -> str:
    return hashlib.sha256(command_fingerprint.encode()).hexdigest()[:16]


def workspace_hash(workspace: Path) -> str:
    return hashlib.sha256(str(workspace).encode()).hexdigest()[:16]


def is_restart(command: str) -> bool:
    return RESTART_ENV in command


def numbers_in_command(command: str) -> set[int]:
    return {int(value) for value in re.findall(r"(?<!\d)(\d{2,5})(?!\d)", command)}


def parse_port(command: str) -> int | None:
    patterns = [
        r"(?:--port|-p)\s*=?\s*([0-9]{2,5})\b",
        r"\bPORT=([0-9]{2,5})\b",
        r"localhost:([0-9]{2,5})\b",
        r"127\.0\.0\.1:([0-9]{2,5})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            return int(match.group(1))
    return None


def port_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
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


def listening_ports() -> set[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(port) for port in re.findall(r":(\d{2,5})\s+\(LISTEN\)", result.stdout)}


def learned_start(learned: dict[str, Any], command_fingerprint: str) -> dict[str, Any] | None:
    return next((item for item in learned["starts"] if item.get("fingerprint") == command_fingerprint), None)


def learned_stop(learned: dict[str, Any], command_fingerprint: str) -> dict[str, Any] | None:
    return next((item for item in learned["stops"] if item.get("fingerprint") == command_fingerprint), None)


def upsert(items: list[dict[str, Any]], command_fingerprint: str, **fields: Any) -> None:
    item = next((entry for entry in items if entry.get("fingerprint") == command_fingerprint), None)
    if item is None:
        item = {"fingerprint": command_fingerprint, "hits": 0}
        items.append(item)
    item["hits"] = int(item.get("hits", 0)) + 1
    item["last_seen"] = now()
    item.update(fields)


def learn_start(workspace: Path, command_fingerprint: str, port: int) -> None:
    learned = load_learned(workspace)
    upsert(learned["starts"], command_fingerprint, last_port=port)
    save_learned(workspace, learned)


def learn_stop(workspace: Path, command_fingerprint: str) -> None:
    learned = load_learned(workspace)
    upsert(learned["stops"], command_fingerprint)
    save_learned(workspace, learned)


def seed_start(command: str) -> bool:
    return bool(START_RE.search(command)) and not EXCLUDE_RE.search(command)


def stop_by_live_number(command: str, servers: dict[str, Any]) -> bool:
    command_numbers = numbers_in_command(command)
    for server in servers.values():
        if server.get("state") != "live":
            continue
        pid = server.get("pid")
        live_numbers = {int(server["port"]), *([int(pid)] if pid else [])}
        if command_numbers & live_numbers:
            return True
    return False


def is_stop_command(command: str, learned: dict[str, Any], servers: dict[str, Any]) -> bool:
    command_fingerprint = fingerprint(command)
    return bool(STOP_RE.search(command)) or learned_stop(learned, command_fingerprint) is not None or stop_by_live_number(command, servers)


def is_start_command(command: str, learned: dict[str, Any]) -> bool:
    command_fingerprint = fingerprint(command)
    return learned_start(learned, command_fingerprint) is not None or seed_start(command)


def prune_closed(workspace: Path) -> None:
    if not state_path(workspace).exists():
        return
    with state_lock(workspace):
        data = load_state(workspace)
        changed = False
        for key, server in data["servers"].copy().items():
            if server.get("state") != "live":
                continue
            port = int(server["port"])
            if not port_open(port) or not pid_alive(server.get("pid")):
                data["servers"].pop(key, None)
                changed = True
        if changed:
            save_state(workspace, data)


def server_entry(workspace: Path, command: str, port: int, pid: int | None = None, state: str = "live", reason: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "state": state,
        "port": port,
        "name": fingerprint(command),
        "command": normalize_command(command),
        "fingerprint": fingerprint(command),
        "cwd": str(workspace),
    }
    if state == "live":
        entry.update(url=f"http://localhost:{port}", pid=pid if pid is not None else pid_for_port(port), since=now())
    else:
        entry.update(at=now(), reason=(reason or "Failed to start")[:500])
    return entry


def set_live(workspace: Path, command: str, port: int, pid: int | None = None) -> None:
    command_fingerprint = fingerprint(command)
    with state_lock(workspace):
        data = load_state(workspace)
        data["servers"][str(port)] = server_entry(workspace, command, port, pid=pid)
        save_state(workspace, data)
        learned = load_learned(workspace)
        upsert(learned["starts"], command_fingerprint, last_port=port)
        save_learned(workspace, learned)


def set_failed(workspace: Path, command: str, port: int | None, reason: str) -> None:
    key = str(port) if port else f"failed:{command_hash(fingerprint(command))}"
    with state_lock(workspace):
        data = load_state(workspace)
        data["servers"][key] = server_entry(workspace, command, port or 0, state="failed", reason=reason)
        save_state(workspace, data)


def remove_server(workspace: Path, server_id: str | int) -> None:
    key = str(server_id)
    with state_lock(workspace):
        data = load_state(workspace)
        data["servers"].pop(key, None)
        save_state(workspace, data)


def conflict_server(workspace: Path, command: str, learned: dict[str, Any]) -> dict[str, Any] | None:
    command_fingerprint = fingerprint(command)
    explicit_port = parse_port(command)
    learned_item = learned_start(learned, command_fingerprint)
    port = explicit_port or (int(learned_item["last_port"]) if learned_item and learned_item.get("last_port") else None)
    data = load_state(workspace)
    if port is not None:
        server = data["servers"].get(str(port))
        if server and server.get("state") == "live" and port_open(port):
            return server
        if port_open(port):
            return server_entry(workspace, command, port)
    return None


def format_deny(server: dict[str, Any], command: str) -> dict[str, str]:
    pid = server.get("pid")
    kill = f"Run: kill {pid}" if pid else "Run: stop the process using the PID for this port"
    url = server.get("url") or f"http://localhost:{server['port']}"
    message = "\n".join(
        [
            "Another server is already running.",
            "",
            f"- Local:  {url}",
            f"- PID:    {pid or 'unknown'}",
            f"- Dir:    {server.get('cwd', '')}",
            f"- Cmd:    {server.get('command', server.get('name', 'unknown'))}",
            "",
            kill,
            f"Or restart: {RESTART_ENV} {normalize_command(command)}",
        ]
    )
    return {
        "permission": "deny",
        "user_message": message,
        "agent_message": f"{message}\n\nReuse the Local URL, or stop the PID before starting another server.",
    }


def token_path(workspace: Path, command_fingerprint: str, token: str) -> Path:
    return TOKEN_ROOT / f"{workspace_hash(workspace)}-{command_hash(command_fingerprint)}-{token}.json"


def active_probe_paths(workspace: Path) -> list[Path]:
    return list(TOKEN_ROOT.glob(f"{workspace_hash(workspace)}-*.json"))


def begin_probe(workspace: Path, command: str, baseline_ports: set[int] | None = None, explicit_port: int | None = None) -> str:
    TOKEN_ROOT.mkdir(parents=True, exist_ok=True)
    command_fingerprint = fingerprint(command)
    cancel_probe(workspace, command_fingerprint)
    token = secrets.token_hex(8)
    token_path(workspace, command_fingerprint, token).write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "command": command,
                "fingerprint": command_fingerprint,
                "baseline_ports": sorted(baseline_ports or []),
                "explicit_port": explicit_port,
                "started": now(),
            }
        )
    )
    return token


def read_probe(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def probe_active(workspace: Path, command_fingerprint: str, token: str) -> bool:
    return token_path(workspace, command_fingerprint, token).exists()


def cancel_probe(workspace: Path, command_fingerprint: str | None = None, port: int | None = None, token: str | None = None) -> None:
    for path in active_probe_paths(workspace):
        data = read_probe(path)
        if not data:
            path.unlink(missing_ok=True)
            continue
        if command_fingerprint and data.get("fingerprint") != command_fingerprint:
            continue
        if port is not None and data.get("explicit_port") != port:
            continue
        if token and not path.name.endswith(f"-{token}.json"):
            continue
        path.unlink(missing_ok=True)


def finish_probe(workspace: Path, command: str, port: int, token: str, state: str, reason: str | None = None) -> bool:
    command_fingerprint = fingerprint(command)
    if not probe_active(workspace, command_fingerprint, token):
        return False
    cancel_probe(workspace, command_fingerprint, token=token)
    if state == "live":
        set_live(workspace, command, port)
    else:
        set_failed(workspace, command, port, reason or "Failed to start")
    return True


def launch_probe(workspace: Path, command: str, explicit_port: int | None, timeout: int = PROBE_TIMEOUT_SECONDS) -> None:
    token = begin_probe(workspace, command, listening_ports(), explicit_port)
    subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--probe", str(workspace), token, str(timeout)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_probe_port(explicit_port: int | None, baseline_ports: set[int]) -> int | None:
    if explicit_port and port_open(explicit_port):
        return explicit_port
    for port in sorted(listening_ports() - baseline_ports):
        if port_open(port):
            return port
    return None


def probe(workspace: Path, token: str, timeout: int) -> int:
    path = next((item for item in active_probe_paths(workspace) if item.name.endswith(f"-{token}.json")), None)
    data = read_probe(path) if path else None
    if not path or not data:
        return 0
    command = str(data["command"])
    command_fingerprint = str(data["fingerprint"])
    explicit_port = data.get("explicit_port")
    baseline_ports = {int(port) for port in data.get("baseline_ports", [])}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not probe_active(workspace, command_fingerprint, token):
            return 0
        port = open_probe_port(int(explicit_port) if explicit_port else None, baseline_ports)
        if port:
            finish_probe(workspace, command, port, token, "live")
            return 0
        time.sleep(0.5)
    if probe_active(workspace, command_fingerprint, token):
        reason = f"Server did not open a new listening port within {timeout} seconds"
        finish_probe(workspace, command, int(explicit_port) if explicit_port else 0, token, "failed", reason)
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


def matching_server_keys(command: str, servers: dict[str, Any]) -> list[str]:
    command_numbers = numbers_in_command(command)
    keys: list[str] = []
    for key, server in servers.items():
        if server.get("state") != "live":
            continue
        pid = server.get("pid")
        live_numbers = {int(server["port"]), *([int(pid)] if pid else [])}
        if command_numbers & live_numbers:
            keys.append(key)
    return keys


def handle_stop(workspace: Path, command: str) -> None:
    command_fingerprint = fingerprint(command)
    with state_lock(workspace):
        data = load_state(workspace)
        keys = matching_server_keys(command, data["servers"])
        for key in keys:
            cancel_probe(workspace, port=int(data["servers"][key]["port"]))
            data["servers"].pop(key, None)
        if keys:
            save_state(workspace, data)
            learned = load_learned(workspace)
            upsert(learned["stops"], command_fingerprint)
            save_learned(workspace, learned)


def handle_before_shell(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command") or ""
    workspace = workspace_root(payload)
    prune_closed(workspace)
    learned = load_learned(workspace)
    servers = load_state(workspace)["servers"]
    startish = is_start_command(command, learned)
    if not startish and is_stop_command(command, learned, servers):
        return {"permission": "allow"}
    if not startish:
        return {"permission": "allow"}

    conflict = conflict_server(workspace, command, learned)
    if conflict and not is_restart(command):
        return format_deny(conflict, command)
    if conflict and is_restart(command):
        remove_server(workspace, int(conflict["port"]))
    launch_probe(workspace, command, parse_port(command))
    return {"permission": "allow"}


def handle_post_tool(payload: dict[str, Any]) -> None:
    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = workspace_root(payload)
    result = hook_result(payload)
    learned = load_learned(workspace)
    servers = load_state(workspace)["servers"]
    if result.get("exitCode") not in (None, 0) and is_start_command(command, learned):
        for path in active_probe_paths(workspace):
            data = read_probe(path)
            if data and data.get("fingerprint") == fingerprint(command):
                token = path.name.rsplit("-", 1)[-1].removesuffix(".json")
                finish_probe(workspace, command, parse_port(command) or data.get("explicit_port") or 0, token, "failed", failure_reason(result))
        return
    if result.get("exitCode") == 0 and is_stop_command(command, learned, servers):
        handle_stop(workspace, command)


def run_wrapped(argv: list[str]) -> int:
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("Usage: livegate.py --run -- <command...>", file=sys.stderr)
        return 2
    workspace = Path.cwd().resolve()
    command = shlex.join(argv)
    prune_closed(workspace)
    learned = load_learned(workspace)
    conflict = conflict_server(workspace, command, learned)
    if conflict:
        print(format_deny(conflict, command)["user_message"], file=sys.stderr)
        return 1
    baseline = listening_ports()
    process = subprocess.Popen(argv)
    token = begin_probe(workspace, command, baseline, parse_port(command))
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    try:
        while process.poll() is None and time.monotonic() < deadline:
            port = open_probe_port(parse_port(command), baseline)
            if port:
                finish_probe(workspace, command, port, token, "live")
                break
            if not probe_active(workspace, fingerprint(command), token):
                break
            time.sleep(0.5)
        return process.wait()
    finally:
        cancel_probe(workspace, fingerprint(command), token=token)
        for key, server in load_state(workspace)["servers"].copy().items():
            if server.get("pid") == process.pid:
                remove_server(workspace, key)


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--probe":
        return probe(Path(sys.argv[2]), sys.argv[3], PROBE_TIMEOUT_SECONDS)
    if len(sys.argv) == 5 and sys.argv[1] == "--probe":
        return probe(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--run":
        return run_wrapped(sys.argv[2:])

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
