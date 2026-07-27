#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
from http.client import HTTPException
import json
import re
import secrets
import shlex
import ssl
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

STATE_VERSION = 2
STARTING_SECONDS = 60
STATE_NAME = "servers.json"
CONFIG_NAME = "livegate.json"
IGNORE_ENTRY = "/.livegate/"
LOCAL_URL = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{2,5})?(?:/[^\s\x1b]*)?",
    re.IGNORECASE,
)
ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workspace_root(payload: dict[str, Any]) -> Path:
    roots = payload.get("workspace_roots") or []
    return Path(roots[0] if roots else payload.get("cwd") or ".").expanduser().resolve()


def state_path(workspace: Path) -> Path:
    return workspace / ".livegate" / STATE_NAME


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "applications": {}, "attempts": []}


def load_state(workspace: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_path(workspace).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_state()
    if state.get("version") != STATE_VERSION:
        return empty_state()
    state.setdefault("applications", {})
    state.setdefault("attempts", [])
    return state


def ensure_gitignore(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        return
    path = workspace / ".gitignore"
    content = path.read_bytes() if path.exists() else b""
    if IGNORE_ENTRY in content.decode(errors="ignore").splitlines():
        return
    separator = b"" if not content or content.endswith((b"\n", b"\r")) else b"\n"
    with path.open("ab") as handle:
        handle.write(separator + f"{IGNORE_ENTRY}\n".encode())


@contextmanager
def state_lock(workspace: Path):
    directory = workspace / ".livegate"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_state(workspace: Path, state: dict[str, Any]) -> None:
    path = state_path(workspace)
    ensure_gitignore(workspace)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    while tokens and ENVIRONMENT_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    return tokens


def normalize_command(command: str) -> str:
    return shlex.join(command_tokens(command))


def display_command(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "<unparseable command>"
    return shlex.join(
        [
            f"{token.split('=', 1)[0]}=<redacted>"
            if ENVIRONMENT_ASSIGNMENT.match(token)
            else token
            for token in tokens
        ]
    )


def command_hash(command: str) -> str:
    return hashlib.sha256(normalize_command(command).encode()).hexdigest()


def package_script(command: str) -> str | None:
    tokens = command_tokens(command)
    if not tokens or tokens[0] != "pnpm":
        return None
    tokens = tokens[1:]
    package = "."
    for flag in ("--filter", "-F"):
        if flag in tokens:
            index = tokens.index(flag)
            if index + 1 >= len(tokens):
                return None
            package = tokens[index + 1]
            del tokens[index : index + 2]
            break
    if tokens and tokens[0] == "run":
        tokens.pop(0)
    return f"{package}:{tokens[0]}" if tokens and not tokens[0].startswith("-") else None


def load_applications(workspace: Path) -> list[dict[str, Any]]:
    try:
        config = json.loads((workspace / CONFIG_NAME).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    applications = config.get("applications", [])
    return applications if isinstance(applications, list) else []


def configured_application(workspace: Path, command: str) -> dict[str, Any] | None:
    normalized = normalize_command(command)
    script = package_script(command)
    for application in load_applications(workspace):
        commands = [normalize_command(value) for value in application.get("commands", [])]
        scripts = application.get("packageScripts", [])
        if normalized in commands or (script and script in scripts):
            application_id = application.get("id")
            if isinstance(application_id, str) and application_id:
                return {
                    "id": application_id,
                    "name": application.get("name", application_id),
                }
    return None


def append_attempt(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    state["attempts"].append(attempt)
    del state["attempts"][:-50]


def endpoint_open(url: str) -> bool:
    try:
        with urlopen(
            url,
            timeout=0.2,
            context=ssl._create_unverified_context(),
        ) as response:
            response.read(1)
    except HTTPError:
        return True
    except (HTTPException, OSError, ValueError):
        return False
    return True


def advertised_urls(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [url for item in value.values() for url in advertised_urls(item)]
    if isinstance(value, list):
        return [url for item in value for url in advertised_urls(item)]
    if not isinstance(value, str):
        return []
    return [match.group(0).rstrip(".,);") for match in LOCAL_URL.finditer(value)]


def hook_result(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("tool_output")
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"stdout": output}
    return output if isinstance(output, dict) else {}


def duplicate_result(application: dict[str, Any]) -> dict[str, str]:
    name = application["name"]
    if application["state"] == "live":
        message = f"{name} is already running at {application['url']}. Reuse it instead."
    else:
        message = f"{name} is already starting. Reuse it instead."
    return {
        "permission": "deny",
        "user_message": message,
        "agent_message": f"Reuse the existing {name} server; do not start another instance.",
    }


def handle_before_shell(payload: dict[str, Any]) -> dict[str, str]:
    command = payload.get("command") or ""
    workspace = workspace_root(payload)
    configured = configured_application(workspace, command)
    if not configured:
        return {"permission": "allow"}

    fingerprint = command_hash(command)
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(configured["id"])
        if current and (
            (
                current["state"] == "starting"
                and current.get("reservedUntil", 0) > time.time()
            )
            or (current["state"] == "live" and endpoint_open(current["url"]))
        ):
            append_attempt(
                state,
                {
                    "applicationId": configured["id"],
                    "at": now(),
                    "command": display_command(command),
                    "commandHash": fingerprint,
                    "id": secrets.token_hex(8),
                    "state": "denied",
                },
            )
            save_state(workspace, state)
            return duplicate_result(current)

        attempt_id = secrets.token_hex(8)
        attempt = {
            "applicationId": configured["id"],
            "at": now(),
            "command": display_command(command),
            "commandHash": fingerprint,
            "id": attempt_id,
            "state": "starting",
        }
        state["applications"][configured["id"]] = {
            "attemptId": attempt_id,
            "commandHash": fingerprint,
            "name": configured["name"],
            "reservedUntil": time.time() + STARTING_SECONDS,
            "state": "starting",
        }
        append_attempt(state, attempt)
        save_state(workspace, state)
    return {"permission": "allow"}


def handle_post_tool(payload: dict[str, Any]) -> None:
    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = workspace_root(payload)
    configured = configured_application(workspace, command)
    if not configured:
        return

    result = hook_result(payload)
    url = next((value for value in advertised_urls(result) if endpoint_open(value)), None)
    fingerprint = command_hash(command)
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(configured["id"])
        if not current or current.get("commandHash") != fingerprint:
            return
        attempt = next(
            (
                value
                for value in reversed(state["attempts"])
                if value.get("id") == current.get("attemptId")
            ),
            None,
        )
        if url:
            current.update(
                {
                    "since": now(),
                    "state": "live",
                    "url": url,
                }
            )
            if attempt:
                attempt["state"] = "live"
        elif result.get("exitCode") not in (None, 0):
            state["applications"].pop(configured["id"], None)
            if attempt:
                attempt.update(
                    {
                        "state": "failed",
                        "reason": f"Shell exited with code {result.get('exitCode')}",
                    }
                )
        save_state(workspace, state)


def main() -> int:
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
