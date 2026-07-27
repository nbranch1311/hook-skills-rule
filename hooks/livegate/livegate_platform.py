from __future__ import annotations

import hashlib
from http.client import HTTPException
import platform
import re
import ssl
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from livegate_commands import redact_text

LOCAL_URL = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{2,5})?(?:/[^\s\x1b]*)?",
    re.IGNORECASE,
)


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
