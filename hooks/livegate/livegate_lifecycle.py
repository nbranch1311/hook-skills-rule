from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from livegate_commands import command_hash, display_command
from livegate_identity import alias_key, startup_timeout
from livegate_platform import (
    advertised_urls,
    attributed_advertised,
    attributed_candidate,
    instance_healthy,
    listener_pids,
    listener_snapshot,
    process_group,
)
from livegate_state import (
    append_attempt,
    find_attempt,
    load_state,
    now,
    save_state,
    state_lock,
)


def observer_entrypoint() -> Path:
    return Path(__file__).with_name("livegate.py").resolve()


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
            str(observer_entrypoint()),
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
            str(observer_entrypoint()),
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
