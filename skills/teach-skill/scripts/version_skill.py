#!/usr/bin/env python3
"""Version a user-owned Agent Skill without exposing archived copies."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IGNORED_NAMES = {"__pycache__", ".DS_Store"}
MANIFEST_NAME = "manifest.json"
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
NO_CANDIDATE = "No candidate exists"


class SkillError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def resolve_skill(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir() or not (path / "SKILL.md").is_file():
        raise SkillError(f"Not a skill directory: {path}")
    return path


def state_dir(skill: Path) -> Path:
    # A sibling of skills/, never a descendant: agents cannot discover archives.
    return skill.parent.parent / ".teach-skill" / skill.name


def manifest_path(skill: Path) -> Path:
    return state_dir(skill) / MANIFEST_NAME


def version_dir(skill: Path, version: int) -> Path:
    return state_dir(skill) / "versions" / f"{version:04d}"


def copy_skill(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SkillError(f"Destination already exists: {destination}")
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(*IGNORED_NAMES, "*.pyc"),
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def load_manifest(skill: Path) -> dict[str, Any]:
    path = manifest_path(skill)
    if not path.exists():
        initialize(skill)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"Invalid version manifest: {path}: {error}") from error


def initialize(skill: Path) -> None:
    path = manifest_path(skill)
    if path.exists():
        return

    ignore_file = state_dir(skill).parent / ".gitignore"
    if not ignore_file.exists():
        ignore_file.parent.mkdir(parents=True, exist_ok=True)
        ignore_file.write_text("*\n", encoding="utf-8")
    first_version = version_dir(skill, 1)
    first_version.parent.mkdir(parents=True, exist_ok=True)
    copy_skill(skill, first_version)
    write_json_atomic(
        path,
        {
            "activePath": str(skill),
            "activeVersion": 1,
            "skill": skill.name,
            "versions": [
                {
                    "createdAt": now(),
                    "example": "",
                    "lesson": "Initial imported version",
                    "version": 1,
                }
            ],
        },
    )


def frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        raise SkillError("SKILL.md must start with YAML frontmatter")
    closing = text.find("\n---\n", 4)
    if closing == -1:
        raise SkillError("SKILL.md frontmatter is not closed")
    return text[4:closing]


def active_base_for(candidate: Path) -> Path:
    possible_manifest = candidate.parent / MANIFEST_NAME
    if candidate.name == "candidate" and possible_manifest.is_file():
        data = json.loads(possible_manifest.read_text(encoding="utf-8"))
        return Path(data["activePath"])
    return candidate


def broken_links(path: Path, base: Path) -> list[str]:
    errors: list[str] = []
    for markdown in path.rglob("*.md"):
        relative_parent = markdown.relative_to(path).parent
        for target in LINK_PATTERN.findall(markdown.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith(("mailto:", "/")):
                continue
            resolved = (base / relative_parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{markdown.relative_to(path)}: missing link {target}")
    return errors


def validate_skill(
    path: Path, expected_name: str | None = None, link_base: Path | None = None
) -> list[str]:
    skill_file = path / "SKILL.md"
    if not skill_file.is_file():
        raise SkillError(f"Missing SKILL.md: {path}")

    metadata = frontmatter(skill_file.read_text(encoding="utf-8"))
    name_match = re.search(r"(?m)^name:\s*([^\s#]+)", metadata)
    description_match = re.search(r"(?m)^description:\s*(.+)", metadata)
    if not name_match or not NAME_PATTERN.fullmatch(name_match.group(1)):
        raise SkillError("Frontmatter name must use lowercase letters, numbers, and hyphens")
    if not description_match:
        raise SkillError("Frontmatter description is required")
    if expected_name and name_match.group(1) != expected_name:
        raise SkillError(
            f"Frontmatter name {name_match.group(1)!r} does not match {expected_name!r}"
        )

    errors = broken_links(path, link_base or active_base_for(path))
    if errors:
        raise SkillError("Broken skill references:\n" + "\n".join(errors))
    return ["frontmatter", "name", "description", "references"]


def files(path: Path) -> dict[str, bytes | str]:
    result: dict[str, bytes | str] = {}
    for item in sorted(path.rglob("*")):
        if not item.is_file() or any(part in IGNORED_NAMES for part in item.parts):
            continue
        relative = str(item.relative_to(path))
        if item.is_symlink():
            result[relative] = f"SYMLINK:{os.readlink(item)}"
        else:
            result[relative] = item.read_bytes()
    return result


def folder_diff(left: Path, right: Path, left_label: str, right_label: str) -> str:
    left_files = files(left)
    right_files = files(right)
    chunks: list[str] = []
    for name in sorted(left_files.keys() | right_files.keys()):
        before = left_files.get(name)
        after = right_files.get(name)
        if before == after:
            continue
        if isinstance(before, bytes) and isinstance(after, bytes):
            try:
                before_text = before.decode("utf-8").splitlines(keepends=True)
                after_text = after.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                chunks.append(f"Binary files differ: {name}\n")
                continue
            chunks.extend(
                difflib.unified_diff(
                    before_text,
                    after_text,
                    fromfile=f"{left_label}/{name}",
                    tofile=f"{right_label}/{name}",
                )
            )
        elif before is None:
            chunks.append(f"Added: {name}\n")
        elif after is None:
            chunks.append(f"Deleted: {name}\n")
        else:
            chunks.append(f"Changed: {name}\n")
    return "".join(chunks)


def replace_active(skill: Path, source: Path) -> None:
    temporary = skill.parent / f".{skill.name}.teach-skill-new"
    backup = skill.parent / f".{skill.name}.teach-skill-backup"
    if temporary.exists() or backup.exists():
        raise SkillError(f"Stale activation directory beside {skill}")
    copy_skill(source, temporary)
    os.replace(skill, backup)
    try:
        os.replace(temporary, skill)
    except Exception:
        os.replace(backup, skill)
        raise
    shutil.rmtree(backup)


def read_text_file(path: str, label: str) -> str:
    value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not value:
        raise SkillError(f"{label} cannot be empty")
    return value


def command_prepare(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    manifest = load_manifest(skill)
    candidate = state_dir(skill) / "candidate"
    if candidate.exists():
        raise SkillError(
            f"Candidate already exists: {candidate}. Continue it or run discard explicitly."
        )
    copy_skill(skill, candidate)
    emit(
        {
            "activeVersion": manifest["activeVersion"],
            "candidate": str(candidate),
            "skill": str(skill),
        }
    )


def command_discard(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    candidate = state_dir(skill) / "candidate"
    if not candidate.exists():
        raise SkillError(NO_CANDIDATE)
    shutil.rmtree(candidate)
    emit({"discarded": str(candidate)})


def command_validate(args: argparse.Namespace) -> None:
    path = Path(args.skill).expanduser().resolve()
    if not path.is_dir():
        raise SkillError(f"Not a directory: {path}")
    expected = None
    possible_manifest = path.parent / MANIFEST_NAME
    if path.name == "candidate" and possible_manifest.is_file():
        expected = json.loads(possible_manifest.read_text(encoding="utf-8"))["skill"]
    checks = validate_skill(path, expected)
    emit({"checks": checks, "path": str(path), "valid": True})


def command_diff(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    candidate = state_dir(skill) / "candidate"
    if not candidate.is_dir():
        raise SkillError(NO_CANDIDATE)
    difference = folder_diff(skill, candidate, "active", "candidate")
    if not difference:
        raise SkillError("Candidate is identical to the active skill")
    print(difference, end="" if difference.endswith("\n") else "\n")


def command_promote(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    manifest = load_manifest(skill)
    candidate = state_dir(skill) / "candidate"
    if not candidate.is_dir():
        raise SkillError(NO_CANDIDATE)

    validate_skill(candidate, skill.name)
    if not folder_diff(skill, candidate, "active", "candidate"):
        raise SkillError("Candidate is identical to the active skill")

    lesson = read_text_file(args.lesson_file, "Lesson")
    example = read_text_file(args.example_file, "Regression example")
    new_version = max(item["version"] for item in manifest["versions"]) + 1
    destination = version_dir(skill, new_version)
    copy_skill(candidate, destination)
    replace_active(skill, candidate)

    manifest["activeVersion"] = new_version
    manifest["versions"].append(
        {
            "createdAt": now(),
            "example": example,
            "lesson": lesson,
            "version": new_version,
        }
    )
    write_json_atomic(manifest_path(skill), manifest)
    shutil.rmtree(candidate)
    emit(
        {
            "activeVersion": new_version,
            "lesson": lesson,
            "previousVersion": new_version - 1,
            "skill": str(skill),
            "validation": "passed",
        }
    )


def command_history(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    manifest = load_manifest(skill)
    emit(
        {
            "activeVersion": manifest["activeVersion"],
            "skill": str(skill),
            "versions": manifest["versions"],
        }
    )


def requested_version(skill: Path, raw_version: str) -> tuple[int, Path]:
    try:
        version = int(raw_version)
    except ValueError as error:
        raise SkillError(f"Invalid version: {raw_version}") from error
    path = version_dir(skill, version)
    if not path.is_dir():
        raise SkillError(f"Version {version} does not exist")
    return version, path


def command_compare(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    load_manifest(skill)
    left_version, left = requested_version(skill, args.version_a)
    right_version, right = requested_version(skill, args.version_b)
    difference = folder_diff(
        left, right, f"version-{left_version}", f"version-{right_version}"
    )
    print(difference or "No differences.\n", end="")


def command_restore(args: argparse.Namespace) -> None:
    skill = resolve_skill(args.skill)
    manifest = load_manifest(skill)
    candidate = state_dir(skill) / "candidate"
    if candidate.exists():
        raise SkillError("A candidate exists; promote or discard it before restoring")
    restored_from, source = requested_version(skill, args.version)
    validate_skill(source, skill.name, skill)

    new_version = max(item["version"] for item in manifest["versions"]) + 1
    copy_skill(source, version_dir(skill, new_version))
    replace_active(skill, source)
    manifest["activeVersion"] = new_version
    manifest["versions"].append(
        {
            "createdAt": now(),
            "example": "",
            "lesson": args.lesson,
            "restoredFrom": restored_from,
            "version": new_version,
        }
    )
    write_json_atomic(manifest_path(skill), manifest)
    emit(
        {
            "activeVersion": new_version,
            "restoredFrom": restored_from,
            "skill": str(skill),
            "validation": "passed",
        }
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("skill")
    prepare.set_defaults(run=command_prepare)

    discard = commands.add_parser("discard")
    discard.add_argument("skill")
    discard.set_defaults(run=command_discard)

    validate = commands.add_parser("validate")
    validate.add_argument("skill")
    validate.set_defaults(run=command_validate)

    diff = commands.add_parser("diff")
    diff.add_argument("skill")
    diff.set_defaults(run=command_diff)

    promote = commands.add_parser("promote")
    promote.add_argument("skill")
    promote.add_argument("--lesson-file", required=True)
    promote.add_argument("--example-file", required=True)
    promote.set_defaults(run=command_promote)

    history = commands.add_parser("history")
    history.add_argument("skill")
    history.set_defaults(run=command_history)

    compare = commands.add_parser("compare")
    compare.add_argument("skill")
    compare.add_argument("version_a")
    compare.add_argument("version_b")
    compare.set_defaults(run=command_compare)

    restore = commands.add_parser("restore")
    restore.add_argument("skill")
    restore.add_argument("version")
    restore.add_argument("--lesson", required=True)
    restore.set_defaults(run=command_restore)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.run(args)
        return 0
    except (OSError, SkillError, json.JSONDecodeError) as error:
        print(f"teach-skill: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
