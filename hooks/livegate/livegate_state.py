from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

STATE_VERSION = 3
STATE_NAME = "servers.json"
IGNORE_ENTRY = "/.livegate/"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def append_attempt(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    state["attempts"].append(attempt)
    del state["attempts"][:-50]


def find_attempt(state: dict[str, Any], attempt_id: str) -> dict[str, Any] | None:
    return next(
        (
            attempt
            for attempt in reversed(state["attempts"])
            if attempt.get("id") == attempt_id
        ),
        None,
    )
