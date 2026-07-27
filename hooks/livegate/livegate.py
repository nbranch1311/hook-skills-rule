#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
from http.client import HTTPException
import json
import platform
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
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
SECRET = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:token|key|secret|password)[A-Za-z0-9_]*)=([^\s]+)",
)


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


def parse_lsof(output: str) -> dict[int, int]:
    listeners: dict[int, int] = {}
    pid: int | None = None
    for line in output.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif pid and line.startswith("n"):
            match = re.search(r":(\d+)$", line)
            if match:
                listeners[int(match.group(1))] = pid
    return listeners


def parse_ss(output: str) -> dict[int, int]:
    listeners: dict[int, int] = {}
    for line in output.splitlines():
        fields = line.split()
        pid = re.search(r"\bpid=(\d+)", line)
        port = re.search(r":(\d+)$", fields[3]) if len(fields) > 3 else None
        if pid and port:
            listeners[int(port.group(1))] = int(pid.group(1))
    return listeners


def parse_proc_started(stat: str) -> str | None:
    fields = stat.rsplit(")", 1)
    if len(fields) != 2:
        return None
    process_fields = fields[1].split()
    return process_fields[19] if len(process_fields) > 19 else None


def listener_pids() -> dict[int, int]:
    if platform.system() == "Darwin":
        command = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"]
        parser = parse_lsof
    elif platform.system() == "Linux":
        command = ["ss", "-ltnpH"]
        parser = parse_ss
    else:
        return {}
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parser(result.stdout)


def process_started(pid: int) -> str | None:
    if platform.system() == "Linux":
        try:
            return parse_proc_started(Path(f"/proc/{pid}/stat").read_text())
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def process_fingerprint(pid: int) -> str | None:
    try:
        if platform.system() == "Linux":
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        else:
            result = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                check=False,
                capture_output=True,
                timeout=1,
            )
            command = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return hashlib.sha256(command.strip()).hexdigest() if command.strip() else None


def listener_snapshot() -> dict[str, dict[str, Any]]:
    return {
        str(port): {
            "fingerprint": process_fingerprint(pid),
            "pid": pid,
            "started": process_started(pid),
        }
        for port, pid in listener_pids().items()
    }


def is_descendant(pid: int, ancestor: int) -> bool:
    for _ in range(32):
        if pid == ancestor:
            return True
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
            )
            parent = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return False
        if not parent.isdigit() or int(parent) <= 1:
            return False
        pid = int(parent)
    return False


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


def startup_timeout(workspace: Path) -> int:
    try:
        config = json.loads((workspace / CONFIG_NAME).read_text())
        return max(1, int(config.get("startupTimeoutSeconds", STARTING_SECONDS)))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return STARTING_SECONDS


def diagnostic(result: dict[str, Any]) -> str | None:
    text = result.get("stderr") or result.get("message")
    if not text:
        return None
    return SECRET.sub(r"\1=<redacted>", " ".join(str(text).split()))[-300:]


def listener_for_url(url: str) -> tuple[int, dict[str, Any]] | None:
    try:
        port = urlsplit(url).port or (443 if url.startswith("https:") else 80)
    except ValueError:
        return None
    pid = listener_pids().get(port)
    if not pid:
        return None
    return port, {
        "fingerprint": process_fingerprint(pid),
        "pid": pid,
        "started": process_started(pid),
    }


def attributed_candidate(application: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    baseline = application.get("listenersBefore", {})
    shell_pid = application.get("shellPid")
    for url in application.get("advertisedUrls", []):
        listener = listener_for_url(url)
        if not listener or not endpoint_open(url):
            continue
        port, identity = listener
        if (
            baseline.get(str(port)) != identity
            and identity.get("started")
            and identity.get("fingerprint")
        ):
            return url, identity

    if not shell_pid:
        return None
    for port, pid in listener_pids().items():
        identity = {
            "fingerprint": process_fingerprint(pid),
            "pid": pid,
            "started": process_started(pid),
        }
        url = f"http://127.0.0.1:{port}/"
        if (
            baseline.get(str(port)) != identity
            and identity.get("started")
            and identity.get("fingerprint")
            and is_descendant(pid, int(shell_pid))
            and endpoint_open(url)
        ):
            return url, identity
    return None


def find_attempt(state: dict[str, Any], attempt_id: str) -> dict[str, Any] | None:
    return next(
        (
            attempt
            for attempt in reversed(state["attempts"])
            if attempt.get("id") == attempt_id
        ),
        None,
    )


def mark_failed(
    workspace: Path,
    application_id: str,
    attempt_id: str,
    reason: str,
) -> None:
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(application_id)
        if not current or current.get("attemptId") != attempt_id:
            return
        state["applications"].pop(application_id)
        attempt = find_attempt(state, attempt_id)
        if attempt:
            attempt.update({"reason": reason[:300], "state": "failed"})
        save_state(workspace, state)


def observe_once(workspace: Path, application_id: str, attempt_id: str) -> bool:
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(application_id)
        if not current or current.get("attemptId") != attempt_id:
            return False
        application = dict(current)
    candidate = attributed_candidate(application)
    if not candidate:
        return False
    url, identity = candidate
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(application_id)
        if not current or current.get("attemptId") != attempt_id:
            return False
        current.update(
            {
                "pid": identity["pid"],
                "processFingerprint": identity["fingerprint"],
                "processStarted": identity["started"],
                "since": now(),
                "state": "live",
                "url": url,
            }
        )
        attempt = find_attempt(state, attempt_id)
        if attempt:
            attempt["state"] = "live"
        save_state(workspace, state)
    return True


def observe(
    workspace: Path,
    application_id: str,
    attempt_id: str,
    timeout: int,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if observe_once(workspace, application_id, attempt_id):
            return 0
        time.sleep(0.2)
    with state_lock(workspace):
        current = load_state(workspace)["applications"].get(application_id, {})
        reason = current.get("failureReason")
    mark_failed(
        workspace,
        application_id,
        attempt_id,
        reason or f"Server did not open within {timeout} seconds",
    )
    return 1


def launch_observer(
    workspace: Path,
    application_id: str,
    attempt_id: str,
    timeout: int,
) -> None:
    subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--observe",
            str(workspace),
            application_id,
            attempt_id,
            str(timeout),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def revalidate(workspace: Path, session: str | None) -> list[str]:
    snapshot = load_state(workspace)
    applications = {
        application_id: dict(application)
        for application_id, application in snapshot["applications"].items()
        if application.get("state") == "live"
    }
    if not applications:
        return []

    listeners = listener_pids()
    health: dict[str, bool] = {}
    for application_id, application in applications.items():
        try:
            port = urlsplit(application["url"]).port or (
                443 if application["url"].startswith("https:") else 80
            )
        except (KeyError, ValueError):
            health[application_id] = False
            continue
        pid = listeners.get(port)
        health[application_id] = bool(
            pid
            and pid == application.get("pid")
            and process_started(pid) == application.get("processStarted")
            and process_fingerprint(pid) == application.get("processFingerprint")
            and endpoint_open(application["url"])
        )

    with state_lock(workspace):
        state = load_state(workspace)
        notices: list[str] = []
        changed = False
        for application_id, was_healthy in health.items():
            application = state["applications"].get(application_id)
            if (
                not application
                or application.get("attemptId")
                != applications[application_id].get("attemptId")
            ):
                continue
            if not was_healthy:
                state["applications"].pop(application_id)
                changed = True
            elif session and application.get("notifiedSession") != session:
                application["notifiedSession"] = session
                notices.append(
                    f"{application['name']} is already running at {application['url']}."
                )
                changed = True
        if changed:
            save_state(workspace, state)
        return notices


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


def allow_result(notices: list[str]) -> dict[str, str]:
    result = {"permission": "allow"}
    if notices:
        result["agent_message"] = " ".join(notices)
    return result


def handle_before_shell(payload: dict[str, Any]) -> dict[str, str]:
    command = payload.get("command") or ""
    workspace = workspace_root(payload)
    notices = revalidate(workspace, payload.get("session_id"))
    configured = configured_application(workspace, command)
    if not configured:
        return allow_result(notices)

    fingerprint = command_hash(command)
    timeout = startup_timeout(workspace)
    listeners_before = listener_snapshot()
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
            "listenersBefore": listeners_before,
            "name": configured["name"],
            "reservedUntil": time.time() + timeout,
            "state": "starting",
        }
        append_attempt(state, attempt)
        save_state(workspace, state)
    return allow_result(notices)


def handle_post_tool(payload: dict[str, Any]) -> None:
    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = workspace_root(payload)
    revalidate(workspace, None)
    configured = configured_application(workspace, command)
    if not configured:
        return

    result = hook_result(payload)
    fingerprint = command_hash(command)
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(configured["id"])
        if not current or current.get("commandHash") != fingerprint:
            return
        attempt_id = current["attemptId"]
        current["advertisedUrls"] = advertised_urls(result)
        if isinstance(result.get("pid"), int):
            current["shellPid"] = result["pid"]
        if reason := diagnostic(result):
            current["failureReason"] = reason
        attempt = find_attempt(state, attempt_id)
        if attempt:
            attempt["shell"] = {
                key: result[key]
                for key in ("exitCode", "status")
                if result.get(key) is not None
            }
        save_state(workspace, state)

    if observe_once(workspace, configured["id"], attempt_id):
        return
    launch_observer(
        workspace,
        configured["id"],
        attempt_id,
        startup_timeout(workspace),
    )


def main() -> int:
    if len(sys.argv) == 6 and sys.argv[1] == "--observe":
        return observe(
            Path(sys.argv[2]),
            sys.argv[3],
            sys.argv[4],
            int(sys.argv[5]),
        )

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
