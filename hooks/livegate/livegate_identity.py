from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from livegate_commands import command_tokens, display_command, normalize_command
from livegate_state import load_state

CONFIG_NAME = "livegate.json"
STARTING_SECONDS = 60


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


def startup_timeout(workspace: Path) -> int:
    try:
        config = json.loads((workspace / CONFIG_NAME).read_text())
        return max(1, int(config.get("startupTimeoutSeconds", STARTING_SECONDS)))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return STARTING_SECONDS
