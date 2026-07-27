from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from livegate_commands import (
    approval_matches,
    command_hash,
    display_command,
    requested_second,
)
from livegate_identity import launch_group, logical_application, startup_timeout
from livegate_lifecycle import (
    deliver_failure_notices,
    failure_notices,
    handle_group_post,
    launch_observer,
    promote_group,
    promote_pending,
    record_pending,
    revalidate,
    revalidate_groups,
)
from livegate_platform import advertised_urls, diagnostic, listener_snapshot, process_group
from livegate_state import append_attempt, find_attempt, load_state, now, save_state, state_lock


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


def hook_result(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("tool_output")
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"stdout": output}
    return output if isinstance(output, dict) else {}


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
