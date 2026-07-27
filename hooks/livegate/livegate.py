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

STATE_VERSION = 3
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
    roots = [
        Path(root).expanduser().resolve()
        for root in payload.get("workspace_roots") or []
    ]
    cwd = command_cwd(payload)
    containing = [root for root in roots if cwd == root or root in cwd.parents]
    return max(containing, key=lambda root: len(root.parts)) if containing else cwd


def command_cwd(payload: dict[str, Any]) -> Path:
    tool_input = payload.get("tool_input") or {}
    cwd = tool_input.get("working_directory") or payload.get("cwd")
    roots = payload.get("workspace_roots") or []
    return Path(cwd or (roots[0] if roots else ".")).expanduser().resolve()


def state_path(workspace: Path) -> Path:
    return workspace / ".livegate" / STATE_NAME


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "applications": {},
        "attempts": [],
        "approvals": {},
        "learned": {},
        "pending": {},
    }


def load_state(workspace: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_path(workspace).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_state()
    if state.get("version") != STATE_VERSION:
        return empty_state()
    state.setdefault("applications", {})
    state.setdefault("attempts", [])
    state.setdefault("approvals", {})
    state.setdefault("learned", {})
    state.setdefault("pending", {})
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


def requested_second(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    prefix = "LIVEGATE_SECOND="
    return next(
        (token[len(prefix) :] for token in tokens if token.startswith(prefix)),
        None,
    )


def approval_matches(
    approval: dict[str, Any] | None,
    workspace: Path,
    application_id: str,
    command_fingerprint: str,
    session: str,
) -> bool:
    return bool(
        approval
        and approval.get("workspace") == str(workspace)
        and approval.get("applicationId") == application_id
        and approval.get("commandHash") == command_fingerprint
        and approval.get("session") == session
        and approval.get("expiresAt", 0) > time.time()
    )


def package_script(command: str) -> str | None:
    package, script = script_invocation(command)
    return f"{package or '.'}:{script}" if script else None


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


def read_package(directory: Path) -> dict[str, Any]:
    try:
        return json.loads((directory / "package.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def nearest_package(workspace: Path, cwd: Path) -> Path:
    directory = cwd
    while directory != workspace and workspace in directory.parents:
        if (directory / "package.json").exists():
            return directory
        directory = directory.parent
    return workspace


def named_packages(workspace: Path, name: str) -> list[Path]:
    matches: list[Path] = []
    for package_json in workspace.rglob("package.json"):
        if "node_modules" in package_json.parts:
            continue
        if read_package(package_json.parent).get("name") == name:
            matches.append(package_json.parent)
    return matches


def script_invocation(command: str) -> tuple[str | None, str | None]:
    tokens = command_tokens(command)
    if not tokens or tokens[0] not in {"npm", "pnpm", "yarn", "bun"}:
        return None, None
    manager = tokens.pop(0)
    package: str | None = None
    if manager == "yarn" and tokens[:1] == ["workspace"] and len(tokens) > 2:
        return tokens[1], tokens[3] if tokens[2] == "run" and len(tokens) > 3 else tokens[2]
    for flag in ("--filter", "-F", "--workspace", "-w"):
        if flag in tokens:
            index = tokens.index(flag)
            if index + 1 < len(tokens):
                package = tokens[index + 1]
                del tokens[index : index + 2]
                break
    if tokens[:1] == ["run"]:
        tokens.pop(0)
    return package, tokens[0] if tokens and not tokens[0].startswith("-") else None


def server_family(command: str) -> str | None:
    tokens = command_tokens(command)
    lowered = [token.lower() for token in tokens]
    if "build" in lowered or "build-storybook" in lowered:
        return None
    if any("storybook" in token and "build-storybook" not in token for token in lowered):
        return "storybook"
    if "vite" in lowered:
        return "vite"
    return None


def config_root(cwd: Path, command: str) -> Path | None:
    tokens = command_tokens(command)
    for flag in ("--config", "--config-dir", "-c"):
        if flag not in tokens:
            continue
        index = tokens.index(flag)
        if index + 1 >= len(tokens):
            return None
        path = Path(tokens[index + 1])
        path = path if path.is_absolute() else cwd / path
        return path.parent if path.suffix or path.name == ".storybook" else path
    return None


def inferred_application(
    workspace: Path,
    cwd: Path,
    command: str,
) -> dict[str, str] | None:
    package_name, script = script_invocation(command)
    matches = named_packages(workspace, package_name) if package_name else []
    if package_name and len(matches) != 1:
        return None
    package = matches[0] if matches else None
    package = package or nearest_package(workspace, config_root(cwd, command) or cwd)
    body = str(read_package(package).get("scripts", {}).get(script, "")) if script else ""

    nested_package, nested_script = script_invocation(body)
    if nested_package:
        nested_matches = named_packages(workspace, nested_package)
        if len(nested_matches) != 1:
            return None
        package = nested_matches[0]
        nested_body = read_package(package).get("scripts", {}).get(nested_script, "")
        family = server_family(str(nested_body)) or server_family(body)
    else:
        family = server_family(body) or server_family(command)
    if not family:
        return None

    try:
        relative = package.relative_to(workspace).as_posix() or "."
    except ValueError:
        return None
    package_label = read_package(package).get("name", relative)
    return {
        "id": f"{relative}:{family}",
        "name": f"{package_label} {family}",
    }


def alias_key(cwd: Path, command: str) -> str:
    value = f"{cwd}\0{normalize_command(command)}"
    return hashlib.sha256(value.encode()).hexdigest()


def logical_application(
    workspace: Path,
    cwd: Path,
    command: str,
) -> dict[str, Any] | None:
    configured = configured_application(workspace, command)
    if configured:
        return configured
    learned = load_state(workspace)["learned"].get(alias_key(cwd, command))
    return learned or inferred_application(workspace, cwd, command)


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
        if current.get("instances"):
            current["state"] = "live"
        else:
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
        instance = {
            "pid": identity["pid"],
            "processFingerprint": identity["fingerprint"],
            "processStarted": identity["started"],
            "shellPid": current.get("shellPid"),
            "since": now(),
            "url": url,
        }
        instances = current.setdefault("instances", [])
        if not any(existing["url"] == url for existing in instances):
            instances.append(instance)
        current["state"] = "live"
        attempt = find_attempt(state, attempt_id)
        if attempt:
            attempt["state"] = "live"
        if current.get("learnKey"):
            state["learned"][current["learnKey"]] = {
                "id": application_id,
                "name": current["name"],
            }
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
        if application.get("instances")
    }
    if not applications:
        return []

    listeners = listener_pids()
    healthy_instances: dict[str, list[dict[str, Any]]] = {}
    for application_id, application in applications.items():
        healthy_instances[application_id] = []
        for instance in application["instances"]:
            try:
                port = urlsplit(instance["url"]).port or (
                    443 if instance["url"].startswith("https:") else 80
                )
            except (KeyError, ValueError):
                continue
            pid = listeners.get(port)
            if (
                pid
                and pid == instance.get("pid")
                and process_started(pid) == instance.get("processStarted")
                and process_fingerprint(pid) == instance.get("processFingerprint")
                and endpoint_open(instance["url"])
            ):
                healthy_instances[application_id].append(instance)

    with state_lock(workspace):
        state = load_state(workspace)
        notices: list[str] = []
        changed = False
        for application_id, instances in healthy_instances.items():
            application = state["applications"].get(application_id)
            if (
                not application
                or application.get("attemptId")
                != applications[application_id].get("attemptId")
            ):
                continue
            if application.get("instances") != instances:
                application["instances"] = instances
                changed = True
            actively_starting = (
                application.get("state") == "starting"
                and application.get("reservedUntil", 0) > time.time()
            )
            if not instances and not actively_starting:
                state["applications"].pop(application_id)
                changed = True
            elif instances and session and application.get("notifiedSession") != session:
                application["notifiedSession"] = session
                notices.append(
                    f"{application['name']} is already running at "
                    + ", ".join(instance["url"] for instance in instances)
                    + "."
                )
                changed = True
        if changed:
            save_state(workspace, state)
        return notices


def record_pending(workspace: Path, cwd: Path, command: str) -> None:
    key = alias_key(cwd, command)
    listeners_before = listener_snapshot()
    reserved_until = time.time() + startup_timeout(workspace)
    with state_lock(workspace):
        state = load_state(workspace)
        state["pending"] = {
            pending_key: pending
            for pending_key, pending in state["pending"].items()
            if pending.get("reservedUntil", 0) > time.time()
        }
        state["pending"][key] = {
            "command": display_command(command),
            "commandHash": command_hash(command),
            "listenersBefore": listeners_before,
            "reservedUntil": reserved_until,
        }
        save_state(workspace, state)


def promote_pending(
    workspace: Path,
    cwd: Path,
    command: str,
    result: dict[str, Any],
) -> dict[str, str] | None:
    key = alias_key(cwd, command)
    urls = advertised_urls(result)
    with state_lock(workspace):
        state = load_state(workspace)
        pending = state["pending"].pop(key, None)
        if not pending or not urls:
            if pending:
                save_state(workspace, state)
            return None
        application = {
            "id": f"fallback:{key[:16]}",
            "name": pending["command"],
        }
        attempt_id = secrets.token_hex(8)
        attempt = {
            "applicationId": application["id"],
            "at": now(),
            "command": pending["command"],
            "commandHash": pending["commandHash"],
            "id": attempt_id,
            "state": "starting",
        }
        state["applications"][application["id"]] = {
            "advertisedUrls": urls,
            "attemptId": attempt_id,
            "commandHash": pending["commandHash"],
            "learnKey": key,
            "listenersBefore": pending["listenersBefore"],
            "name": application["name"],
            "reservedUntil": pending["reservedUntil"],
            "state": "starting",
        }
        append_attempt(state, attempt)
        save_state(workspace, state)
        return application


def duplicate_result(
    application: dict[str, Any],
    approval: str | None = None,
) -> dict[str, str]:
    name = application["name"]
    instances = application.get("instances", [])
    if not instances:
        return {
            "permission": "deny",
            "user_message": f"{name} is already starting. Reuse it instead.",
            "agent_message": f"Reuse the existing {name} launch; do not start another.",
        }
    urls = ", ".join(instance["url"] for instance in instances)
    metadata = "; ".join(
        f"{instance['url']} (pid {instance['pid']}, shell {instance.get('shellPid') or 'unknown'})"
        for instance in instances
    )
    return {
        "permission": "deny",
        "user_message": (
            f"{name} is already running at {urls}. Choose reuse, restart, "
            "or launch a second instance."
        ),
        "agent_message": (
            f"Existing {name}: {metadata}. Ask the user to reuse, restart, or "
            "launch a second instance. For an explicitly approved second instance, "
            f"retry once with LIVEGATE_SECOND={approval}."
        ),
    }


def allow_result(notices: list[str]) -> dict[str, str]:
    result = {"permission": "allow"}
    if notices:
        result["agent_message"] = " ".join(notices)
    return result


def handle_before_shell(payload: dict[str, Any]) -> dict[str, str]:
    command = payload.get("command") or ""
    workspace = workspace_root(payload)
    cwd = command_cwd(payload)
    notices = revalidate(workspace, payload.get("session_id"))
    application = logical_application(workspace, cwd, command)
    if not application:
        record_pending(workspace, cwd, command)
        return allow_result(notices)

    fingerprint = command_hash(command)
    timeout = startup_timeout(workspace)
    listeners_before = listener_snapshot()
    with state_lock(workspace):
        state = load_state(workspace)
        state["approvals"] = {
            token: approval
            for token, approval in state["approvals"].items()
            if approval.get("expiresAt", 0) > time.time()
        }
        current = state["applications"].get(application["id"])
        token = requested_second(command)
        approval = state["approvals"].pop(token, None) if token else None
        approved = approval_matches(
            approval,
            workspace,
            application["id"],
            fingerprint,
            str(payload.get("session_id") or ""),
        )
        duplicate = current and (
            current.get("instances")
            or (
                current["state"] == "starting"
                and current.get("reservedUntil", 0) > time.time()
            )
        )
        if duplicate and not approved:
            approval_token: str | None = None
            if current.get("instances"):
                approval_token = secrets.token_hex(8)
                state["approvals"][approval_token] = {
                    "applicationId": application["id"],
                    "commandHash": fingerprint,
                    "expiresAt": time.time() + 300,
                    "session": str(payload.get("session_id") or ""),
                    "workspace": str(workspace),
                }
            append_attempt(
                state,
                {
                    "applicationId": application["id"],
                    "at": now(),
                    "command": display_command(command),
                    "commandHash": fingerprint,
                    "id": secrets.token_hex(8),
                    "state": "denied",
                },
            )
            save_state(workspace, state)
            return duplicate_result(current, approval_token)

        attempt_id = secrets.token_hex(8)
        attempt = {
            "applicationId": application["id"],
            "at": now(),
            "command": display_command(command),
            "commandHash": fingerprint,
            "id": attempt_id,
            "state": "starting",
        }
        state["applications"][application["id"]] = {
            "attemptId": attempt_id,
            "commandHash": fingerprint,
            "instances": current.get("instances", []) if current else [],
            "listenersBefore": listeners_before,
            "name": application["name"],
            "reservedUntil": time.time() + timeout,
            "state": "starting",
        }
        append_attempt(state, attempt)
        save_state(workspace, state)
    return allow_result(notices)


def handle_post_tool(payload: dict[str, Any]) -> None:
    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = workspace_root(payload)
    cwd = command_cwd(payload)
    revalidate(workspace, None)
    result = hook_result(payload)
    application = logical_application(workspace, cwd, command)
    if not application:
        application = promote_pending(workspace, cwd, command, result)
    if not application:
        return

    fingerprint = command_hash(command)
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(application["id"])
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

    if observe_once(workspace, application["id"], attempt_id):
        return
    launch_observer(
        workspace,
        application["id"],
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
