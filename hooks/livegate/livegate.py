#!/usr/bin/env python3
from __future__ import annotations

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
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

try:
    import fcntl
except ImportError:
    fcntl = None

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
OUTPUT_ASSIGNMENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
SENSITIVE_FLAG = re.compile(r"(?i)^--?(?:api-)?(?:token|key|secret|password|authorization)$")
SENSITIVE_INLINE = re.compile(
    r"(?i)^(--?(?:api-)?(?:token|key|secret|password|authorization))=(.+)$"
)
SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:api_?)?(?:token|key|secret|password|authorization)=)[^&\s]+"
)
BEARER = re.compile(r"(?i)\bBearer\s+\S+")


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
        "groups": {},
        "learned": {},
        "learnedGroups": {},
        "pending": {},
    }


def load_state(workspace: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_path(workspace).read_text())
    except FileNotFoundError:
        return empty_state()
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError("LiveGate state is unreadable") from error
    if state.get("version") != STATE_VERSION:
        return empty_state()
    state.setdefault("applications", {})
    state.setdefault("attempts", [])
    state.setdefault("approvals", {})
    state.setdefault("groups", {})
    state.setdefault("learned", {})
    state.setdefault("learnedGroups", {})
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
        if fcntl is None:
            raise RuntimeError("state locking is unsupported")
        deadline = time.monotonic() + 0.02
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.001)
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
    redacted: list[str] = []
    hide_next = False
    for token in tokens:
        inline = SENSITIVE_INLINE.match(token)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
        elif ENVIRONMENT_ASSIGNMENT.match(token):
            redacted.append(f"{token.split('=', 1)[0]}=<redacted>")
        elif inline:
            redacted.append(f"{inline.group(1)}=<redacted>")
        else:
            redacted.append(SENSITIVE_QUERY.sub(r"\1<redacted>", token))
            hide_next = bool(SENSITIVE_FLAG.match(token))
    return redact_text(shlex.join(redacted))


def redact_text(value: str) -> str:
    value = OUTPUT_ASSIGNMENT.sub(r"\1=<redacted>", value)
    value = SENSITIVE_QUERY.sub(r"\1<redacted>", value)
    return BEARER.sub("Bearer <redacted>", value)


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


def configured_applications(workspace: Path, command: str) -> list[dict[str, Any]]:
    normalized = normalize_command(command)
    script = package_script(command)
    matches: list[dict[str, Any]] = []
    for application in load_applications(workspace):
        commands = [normalize_command(value) for value in application.get("commands", [])]
        scripts = application.get("packageScripts", [])
        if normalized in commands or (script and script in scripts):
            application_id = application.get("id")
            if isinstance(application_id, str) and application_id:
                matches.append(
                    {
                        "endpointIndex": application.get("endpointIndex"),
                        "id": application_id,
                        "name": application.get("name", application_id),
                    }
                )
    return matches


def configured_application(workspace: Path, command: str) -> dict[str, Any] | None:
    matches = configured_applications(workspace, command)
    return matches[0] if len(matches) == 1 else None


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


def launch_group(
    workspace: Path,
    cwd: Path,
    command: str,
) -> dict[str, Any] | None:
    configured = configured_applications(workspace, command)
    if len(configured) > 1:
        identity = ",".join(sorted(application["id"] for application in configured))
        return {
            "applications": configured,
            "id": f"group:{hashlib.sha256(identity.encode()).hexdigest()[:16]}",
            "name": display_command(command),
        }
    learned = load_state(workspace)["learnedGroups"].get(alias_key(cwd, command))
    if learned:
        return {
            "applications": [],
            "expectedMembers": learned["expectedMembers"],
            "id": learned["id"],
            "name": display_command(command),
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


def inspection_command(system: str) -> tuple[list[str], Any] | None:
    if system == "Darwin":
        return ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], parse_lsof
    if system == "Linux":
        return ["ss", "-ltnpH"], parse_ss
    return None


def listener_pids() -> dict[int, int]:
    inspection = inspection_command(platform.system())
    if not inspection:
        raise RuntimeError(f"unsupported platform: {platform.system()}")
    command, parser = inspection
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=0.1,
        )
    except OSError as error:
        raise RuntimeError(f"missing listener tool: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"listener inspection timed out: {command[0]}") from error
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
            timeout=0.1,
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
                timeout=0.1,
            )
            command = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return hashlib.sha256(command.strip()).hexdigest() if command.strip() else None


def process_group(pid: int) -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "pgid=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return int(value) if value.isdigit() else None


def listener_snapshot() -> dict[str, dict[str, Any]]:
    return {
        str(port): {"pid": pid}
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
                timeout=0.1,
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
            timeout=0.05,
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
    return [
        urlunsplit((*urlsplit(match.group(0).rstrip(".,);"))[:3], "", ""))
        for match in LOCAL_URL.finditer(value)
    ]


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
    return redact_text(" ".join(str(text).split()))[-300:]


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


def attributed_advertised(
    application: dict[str, Any],
) -> list[tuple[int, str, dict[str, Any]]]:
    baseline = application.get("listenersBefore", {})
    shell_pid = application.get("shellPid")
    shell_pgid = application.get("shellPgid")
    if not shell_pid:
        return []
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for index, url in enumerate(application.get("advertisedUrls", [])):
        listener = listener_for_url(url)
        if not listener or not endpoint_open(url):
            continue
        port, identity = listener
        if (
            baseline.get(str(port), {}).get("pid") != identity["pid"]
            and identity.get("started")
            and identity.get("fingerprint")
            and (
                is_descendant(identity["pid"], int(shell_pid))
                or (
                    shell_pgid
                    and process_group(identity["pid"]) == int(shell_pgid)
                )
            )
        ):
            candidates.append((index, url, identity))
    return candidates


def attributed_candidate(application: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    advertised = attributed_advertised(application)
    if advertised:
        _, url, identity = advertised[0]
        return url, identity

    baseline = application.get("listenersBefore", {})
    shell_pid = application.get("shellPid")
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
            baseline.get(str(port), {}).get("pid") != identity["pid"]
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
    advertised = attributed_advertised(application)
    if advertised:
        candidates = [(url, identity) for _, url, identity in advertised]
    else:
        fallback = attributed_candidate(application)
        candidates = [fallback] if fallback else []
    if not candidates:
        return False
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["applications"].get(application_id)
        if not current or current.get("attemptId") != attempt_id:
            return False
        instances = current.setdefault("instances", [])
        for url, identity in candidates:
            if any(existing["url"] == url for existing in instances):
                continue
            instances.append(
                {
                    "attemptId": attempt_id,
                    "pid": identity["pid"],
                    "processFingerprint": identity["fingerprint"],
                    "processStarted": identity["started"],
                    "shellPid": current.get("shellPid"),
                    "since": now(),
                    "url": url,
                }
            )
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


def instance_healthy(instance: dict[str, Any], listeners: dict[int, int]) -> bool:
    try:
        port = urlsplit(instance["url"]).port or (
            443 if instance["url"].startswith("https:") else 80
        )
    except (KeyError, ValueError):
        return False
    pid = listeners.get(port)
    return bool(
        pid
        and pid == instance.get("pid")
        and process_started(pid) == instance.get("processStarted")
        and process_fingerprint(pid) == instance.get("processFingerprint")
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
        healthy_instances[application_id] = [
            instance
            for instance in application["instances"]
            if instance_healthy(instance, listeners)
        ]

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


def revalidate_groups(workspace: Path, session: str | None) -> list[str]:
    snapshot = load_state(workspace)
    groups = {
        group_id: dict(group)
        for group_id, group in snapshot["groups"].items()
        if group.get("members")
    }
    if not groups:
        return []
    listeners = listener_pids()
    healthy = {
        group_id: [
            member
            for member in group["members"]
            if instance_healthy(member, listeners)
        ]
        for group_id, group in groups.items()
    }
    with state_lock(workspace):
        state = load_state(workspace)
        notices: list[str] = []
        changed = False
        for group_id, members in healthy.items():
            group = state["groups"].get(group_id)
            if not group or group.get("attemptId") != groups[group_id].get("attemptId"):
                continue
            if group.get("members") != members:
                group["members"] = members
                changed = True
            actively_starting = (
                group.get("state") == "starting"
                and group.get("reservedUntil", 0) > time.time()
            )
            if not members and not actively_starting:
                state["groups"].pop(group_id)
                changed = True
                continue
            current_attempt_members = sum(
                member.get("attemptId") == group.get("attemptId") for member in members
            )
            new_state = (
                "live"
                if current_attempt_members >= group["expectedMembers"]
                else "degraded"
            )
            if members and group.get("state") != new_state:
                group["state"] = new_state
                changed = True
            if members and session and group.get("notifiedSession") != session:
                group["notifiedSession"] = session
                notices.append(
                    f"{group['name']} group is {new_state}: "
                    + ", ".join(member["url"] for member in members)
                    + "."
                )
                changed = True
        if changed:
            save_state(workspace, state)
        return notices


def failure_notices(workspace: Path) -> tuple[list[str], list[str]]:
    with state_lock(workspace):
        state = load_state(workspace)
        notices: list[str] = []
        attempt_ids: list[str] = []
        for attempt in state["attempts"]:
            if attempt.get("state") != "failed" or attempt.get("notified"):
                continue
            identity = attempt.get("applicationId") or attempt.get("groupId") or "launch"
            notices.append(
                f"LiveGate: {identity} failed: {attempt.get('reason', 'startup failed')}."
            )
            attempt_ids.append(attempt["id"])
        return notices, attempt_ids


def deliver_failure_notices(
    workspace: Path,
    attempt_ids: list[str],
    result: dict[str, str],
) -> dict[str, str]:
    if not attempt_ids:
        return result
    with state_lock(workspace):
        state = load_state(workspace)
        for attempt in state["attempts"]:
            if attempt.get("id") in attempt_ids:
                attempt["notified"] = True
        save_state(workspace, state)
    return result


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


def promote_group(
    workspace: Path,
    cwd: Path,
    command: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    urls = advertised_urls(result)
    if len(urls) < 2:
        return None
    key = alias_key(cwd, command)
    with state_lock(workspace):
        state = load_state(workspace)
        pending = state["pending"].pop(key, None)
        if not pending:
            return None
        group_id = f"group:fallback:{key[:16]}"
        attempt_id = secrets.token_hex(8)
        state["groups"][group_id] = {
            "advertisedUrls": urls,
            "applicationIds": [],
            "applicationMappings": [],
            "attemptId": attempt_id,
            "commandHash": pending["commandHash"],
            "expectedMembers": len(urls),
            "learnKey": key,
            "listenersBefore": pending["listenersBefore"],
            "members": [],
            "name": pending["command"],
            "reservedUntil": pending["reservedUntil"],
            "state": "starting",
        }
        append_attempt(
            state,
            {
                "applicationIds": [],
                "at": now(),
                "command": pending["command"],
                "commandHash": pending["commandHash"],
                "groupId": group_id,
                "id": attempt_id,
                "state": "starting",
            },
        )
        save_state(workspace, state)
        return {"applications": [], "id": group_id, "name": pending["command"]}


def group_duplicate_result(group: dict[str, Any], token: str) -> dict[str, str]:
    members = group.get("members", [])
    live = ", ".join(
        f"{member.get('name', 'endpoint')} at {member['url']}" for member in members
    )
    application_ids = group.get("applicationIds", [])
    application_names = group.get("applicationNames", {})
    live_ids = {member.get("applicationId") for member in members}
    failed = [application_id for application_id in application_ids if application_id not in live_ids]
    failure = (
        f" Failed: {', '.join(application_names.get(item, item) for item in failed)}."
        if failed
        else ""
    )
    return {
        "permission": "deny",
        "user_message": (
            f"{group['name']} launch group is {group['state']}. Live: {live}.{failure} "
            "Choose targeted recovery, full restart, or launch a second group."
        ),
        "agent_message": (
            "Do not relaunch automatically. Ask the user which recovery they want. "
            "For an explicitly approved second group attempt, retry once with "
            f"LIVEGATE_SECOND={token}."
        ),
    }


def handle_group_before(
    payload: dict[str, Any],
    workspace: Path,
    command: str,
    group_spec: dict[str, Any],
    notices: list[str],
) -> dict[str, str]:
    fingerprint = command_hash(command)
    applications = group_spec["applications"]
    application_ids = [application["id"] for application in applications]
    listeners_before = listener_snapshot()
    with state_lock(workspace):
        state = load_state(workspace)
        state["approvals"] = {
            token: approval
            for token, approval in state["approvals"].items()
            if approval.get("expiresAt", 0) > time.time()
        }
        current = state["groups"].get(group_spec["id"])
        requested = requested_second(command)
        approval = state["approvals"].pop(requested, None) if requested else None
        approved = approval_matches(
            approval,
            workspace,
            group_spec["id"],
            fingerprint,
            str(payload.get("session_id") or ""),
        )
        duplicate = current and (
            current.get("members")
            or (
                current.get("state") == "starting"
                and current.get("reservedUntil", 0) > time.time()
            )
        )
        if duplicate and not approved:
            token = secrets.token_hex(8)
            state["approvals"][token] = {
                "applicationId": group_spec["id"],
                "commandHash": fingerprint,
                "expiresAt": time.time() + 300,
                "session": str(payload.get("session_id") or ""),
                "workspace": str(workspace),
            }
            append_attempt(
                state,
                {
                    "applicationIds": current.get("applicationIds", []),
                    "at": now(),
                    "command": display_command(command),
                    "commandHash": fingerprint,
                    "groupId": group_spec["id"],
                    "id": secrets.token_hex(8),
                    "state": "denied",
                },
            )
            save_state(workspace, state)
            return group_duplicate_result(current, token)

        attempt_id = secrets.token_hex(8)
        mappings = applications or (current or {}).get("applicationMappings", [])
        expected = (
            len(mappings)
            or (current or {}).get("expectedMembers")
            or group_spec.get("expectedMembers", 0)
        )
        state["groups"][group_spec["id"]] = {
            "applicationIds": application_ids or (current or {}).get("applicationIds", []),
            "applicationNames": (
                {application["id"]: application["name"] for application in applications}
                or (current or {}).get("applicationNames", {})
            ),
            "applicationMappings": mappings,
            "attemptId": attempt_id,
            "commandHash": fingerprint,
            "expectedMembers": expected,
            "listenersBefore": listeners_before,
            "members": (current or {}).get("members", []),
            "name": group_spec["name"],
            "reservedUntil": time.time() + startup_timeout(workspace),
            "state": "starting",
        }
        append_attempt(
            state,
            {
                "applicationIds": application_ids,
                "at": now(),
                "command": display_command(command),
                "commandHash": fingerprint,
                "groupId": group_spec["id"],
                "id": attempt_id,
                "state": "starting",
            },
        )
        save_state(workspace, state)
    return allow_result(notices)


def observe_group(workspace: Path, group_id: str, attempt_id: str) -> bool:
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["groups"].get(group_id)
        if not current or current.get("attemptId") != attempt_id:
            return False
        group = dict(current)
    candidates = attributed_advertised(group)
    if not candidates:
        return False
    mappings = group.get("applicationMappings", [])
    members: list[dict[str, Any]] = []
    for endpoint_index, url, identity in candidates:
        mapping = next(
            (
                application
                for position, application in enumerate(mappings)
                if application.get("endpointIndex", position) == endpoint_index
            ),
            None,
        )
        members.append(
            {
                "applicationId": (
                    mapping["id"] if mapping else f"endpoint-{endpoint_index + 1}"
                ),
                "attemptId": attempt_id,
                "name": (
                    mapping["name"] if mapping else f"Endpoint {endpoint_index + 1}"
                ),
                "pid": identity["pid"],
                "processFingerprint": identity["fingerprint"],
                "processStarted": identity["started"],
                "shellPid": group.get("shellPid"),
                "since": now(),
                "url": url,
            }
        )
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["groups"].get(group_id)
        if not current or current.get("attemptId") != attempt_id:
            return False
        existing = {
            member["url"]: member for member in current.get("members", [])
        }
        existing.update({member["url"]: member for member in members})
        current["members"] = list(existing.values())
        current_attempt_members = sum(
            member.get("attemptId") == attempt_id for member in current["members"]
        )
        current["state"] = (
            "live"
            if current_attempt_members >= current["expectedMembers"]
            else "degraded"
        )
        if current.get("learnKey"):
            state["learnedGroups"][current["learnKey"]] = {
                "expectedMembers": current["expectedMembers"],
                "id": group_id,
            }
        attempt = find_attempt(state, attempt_id)
        if attempt:
            attempt["state"] = current["state"]
        save_state(workspace, state)
        return current["state"] == "live"


def observe_group_until(
    workspace: Path,
    group_id: str,
    attempt_id: str,
    timeout: int,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if observe_group(workspace, group_id, attempt_id):
            return 0
        time.sleep(0.2)
    with state_lock(workspace):
        state = load_state(workspace)
        group = state["groups"].get(group_id)
        if not group or group.get("attemptId") != attempt_id:
            return 0
        attempt = find_attempt(state, attempt_id)
        if group.get("members"):
            group["state"] = "degraded"
            if attempt:
                attempt["state"] = "degraded"
        else:
            state["groups"].pop(group_id)
            if attempt:
                attempt.update(
                    {
                        "reason": f"Launch group did not open within {timeout} seconds",
                        "state": "failed",
                    }
                )
        save_state(workspace, state)
    return 1


def launch_group_observer(
    workspace: Path,
    group_id: str,
    attempt_id: str,
    timeout: int,
) -> None:
    subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--observe-group",
            str(workspace),
            group_id,
            attempt_id,
            str(timeout),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def handle_group_post(
    workspace: Path,
    group_spec: dict[str, Any],
    result: dict[str, Any],
) -> None:
    with state_lock(workspace):
        state = load_state(workspace)
        current = state["groups"].get(group_spec["id"])
        if not current:
            return
        attempt_id = current["attemptId"]
        current["advertisedUrls"] = advertised_urls(result)
        if isinstance(result.get("pid"), int):
            current["shellPid"] = result["pid"]
            current["shellPgid"] = process_group(result["pid"]) or result["pid"]
        attempt = find_attempt(state, attempt_id)
        if attempt:
            attempt["shell"] = {
                key: result[key]
                for key in ("exitCode", "status")
                if result.get(key) is not None
            }
        save_state(workspace, state)
    launch_group_observer(
        workspace,
        group_spec["id"],
        attempt_id,
        startup_timeout(workspace),
    )


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
    notices, failure_attempt_ids = failure_notices(workspace)
    notices.extend(revalidate(workspace, payload.get("session_id")))
    notices.extend(revalidate_groups(workspace, payload.get("session_id")))
    group_spec = launch_group(workspace, cwd, command)
    if group_spec:
        return deliver_failure_notices(
            workspace,
            failure_attempt_ids,
            handle_group_before(payload, workspace, command, group_spec, notices),
        )
    application = logical_application(workspace, cwd, command)
    if not application:
        record_pending(workspace, cwd, command)
        return deliver_failure_notices(
            workspace,
            failure_attempt_ids,
            allow_result(notices),
        )

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
            for attempt in state["attempts"]:
                if attempt.get("id") in failure_attempt_ids:
                    attempt["notified"] = True
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
    return deliver_failure_notices(
        workspace,
        failure_attempt_ids,
        allow_result(notices),
    )


def handle_post_tool(payload: dict[str, Any]) -> None:
    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = workspace_root(payload)
    cwd = command_cwd(payload)
    revalidate(workspace, None)
    revalidate_groups(workspace, None)
    result = hook_result(payload)
    group_spec = launch_group(workspace, cwd, command)
    if not group_spec and len(advertised_urls(result)) > 1:
        group_spec = promote_group(workspace, cwd, command, result)
    if group_spec:
        handle_group_post(workspace, group_spec, result)
        return
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
            current["shellPgid"] = process_group(result["pid"]) or result["pid"]
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

    launch_observer(
        workspace,
        application["id"],
        attempt_id,
        startup_timeout(workspace),
    )


def main() -> int:
    try:
        if len(sys.argv) == 6:
            observer = observe_group_until if sys.argv[1] == "--observe-group" else observe
            timeout = int(sys.argv[5])
            deadline = time.monotonic() + timeout + 1
            while True:
                try:
                    return observer(
                        Path(sys.argv[2]),
                        sys.argv[3],
                        sys.argv[4],
                        max(1, int(deadline - time.monotonic())),
                    )
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)

        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("hook_event_name") == "beforeShellExecution":
            print(json.dumps(handle_before_shell(payload)))
        elif payload.get("hook_event_name") == "postToolUse":
            handle_post_tool(payload)
            print("{}")
        else:
            print("{}")
    except Exception as error:
        print(
            json.dumps(
                {
                    "permission": "allow",
                    "agent_message": (
                        f"LiveGate warning ({type(error).__name__}); command allowed."
                    ),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
