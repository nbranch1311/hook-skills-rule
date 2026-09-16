#!/usr/bin/env python3
"""Snapshot git + worktree state for catch-me-up. Execute; do not read as reference."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PARKED_HINTS = (
    "-control",
    "-treatment",
    "_simulation",
    "pr-review-tree",
    "/experiments",
)
TOOLING_UNTRACKED = {
    ".unlazy",
    "node_modules",
    ".scratch",
    "dist",
    ".vite",
    "coverage",
}


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def config_candidates():
    env = os.environ.get("CATCH_ME_UP_HOME")
    if env:
        yield Path(expand(env))
    yield Path.home() / ".config" / "catch-me-up"
    yield SKILL_DIR


def find_config_file(name: str) -> Path | None:
    for directory in config_candidates():
        path = directory / name
        if path.is_file():
            return path
    return None


def load_json(name: str, default):
    path = find_config_file(name)
    if path is None:
        return path, default
    try:
        return path, json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read {}: {}".format(path, exc), file=sys.stderr)
        sys.exit(1)


def run(args: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        return "ERROR: {}".format(err[-1] if err else result.returncode)
    return result.stdout


def parse_worktrees(primary: str) -> list[dict[str, str]]:
    raw = run(["git", "worktree", "list", "--porcelain"], cwd=primary)
    if raw.startswith("ERROR:"):
        return [{"path": primary, "error": raw}]
    trees = []
    current = {}
    for line in raw.splitlines():
        if not line:
            if current:
                trees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            branch = line[len("branch ") :]
            prefix = "refs/heads/"
            if branch.startswith(prefix):
                branch = branch[len(prefix) :]
            current["branch"] = branch
        elif line == "detached":
            current["detached"] = "true"
        elif line.startswith("bare"):
            current["bare"] = "true"
    if current:
        trees.append(current)
    return trees


def porcelain_summary(cwd: str):
    raw = run(["git", "status", "--porcelain", "-uall"], cwd=cwd)
    if raw.startswith("ERROR:"):
        return -1, -1, -1, Counter()
    staged = unstaged = untracked = 0
    tops = Counter()
    for line in raw.splitlines():
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            untracked += 1
            rel = line[3:].strip()
            tops[rel.split("/")[0]] += 1
        else:
            if x != " ":
                staged += 1
            if y != " ":
                unstaged += 1
    return staged, unstaged, untracked, tops


def untracked_note(tops: Counter) -> str:
    if not tops:
        return ""
    parts = ["{}={}".format(name, count) for name, count in tops.most_common(5)]
    return " (" + ", ".join(parts) + ")"


def meaningful_untracked(tops: Counter) -> int:
    return sum(n for name, n in tops.items() if name not in TOOLING_UNTRACKED)


def last_commit(cwd: str) -> str:
    raw = run(["git", "log", "-1", "--format=%h\t%cI\t%s"], cwd=cwd)
    return raw.strip() or "none"


def upstream(cwd: str) -> str:
    name = run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=cwd).strip()
    if name.startswith("ERROR:"):
        return "none"
    counts = run(
        ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        cwd=cwd,
    ).strip()
    if counts.startswith("ERROR:"):
        return name
    behind, _, ahead = counts.partition("\t")
    if not ahead:
        behind, _, ahead = counts.partition(" ")
    return "{}  ahead {}  behind {}".format(name, ahead.strip(), behind.strip())


def stash_count(cwd: str) -> int:
    raw = run(["git", "stash", "list"], cwd=cwd)
    if raw.startswith("ERROR:") or not raw.strip():
        return 0
    return len(raw.splitlines())


def remote_url(cwd: str) -> str:
    raw = run(["git", "remote", "get-url", "origin"], cwd=cwd).strip()
    return raw if not raw.startswith("ERROR:") else "none"


def is_parked(path: str, detached: bool, dirty: bool) -> bool:
    if dirty:
        return False
    lowered = path.replace("\\", "/")
    hinted = any(hint in lowered for hint in PARKED_HINTS)
    return detached and hinted


def classify_activity(
    dirty: bool, ahead: bool, commit_iso: str, lookback: datetime
) -> str:
    if dirty:
        return "HOT (dirty)"
    if ahead:
        return "HOT (unpushed)"
    if commit_iso:
        try:
            stamp = datetime.fromisoformat(commit_iso.replace("Z", "+00:00"))
            if stamp >= lookback:
                return "RECENT"
        except ValueError:
            pass
    return "quiet"


def inspect_worktree(tree: dict, lookback: datetime) -> list:
    path = tree.get("path", "")
    lines = ["worktree: {}".format(path)]
    if tree.get("error"):
        lines.append("  error: {}".format(tree["error"]))
        return lines
    if tree.get("bare") == "true":
        lines.append("  bare: skip")
        return lines
    if not Path(path).exists():
        lines.append("  missing: path does not exist")
        return lines

    detached = tree.get("detached") == "true"
    branch = tree.get("branch", "DETACHED" if detached else "unknown")
    staged, unstaged, untracked, tops = porcelain_summary(path)
    real_dirty = staged > 0 or unstaged > 0 or meaningful_untracked(tops) > 0
    if staged < 0:
        lines.append("  error: git status failed")
        return lines

    if is_parked(path, detached, real_dirty):
        lines.append("  parked: clean detached experiment/control tree")
        return lines

    commit = last_commit(path)
    hash_, _, rest = commit.partition("\t")
    iso, _, subject = rest.partition("\t")
    tracking = upstream(path)
    ahead = False
    if tracking != "none" and "ahead " in tracking:
        try:
            ahead = int(tracking.split("ahead ", 1)[1].split()[0]) > 0
        except (IndexError, ValueError):
            ahead = False
    activity = classify_activity(real_dirty, ahead, iso, lookback)

    lines.append("  branch: {}".format(branch))
    lines.append("  head: {}  {}  {}".format(hash_, iso, subject))
    if staged or unstaged or untracked:
        lines.append(
            "  dirty: {} staged, {} unstaged, {} untracked{}".format(
                staged, unstaged, untracked, untracked_note(tops)
            )
        )
    else:
        lines.append("  dirty: clean")
    lines.append("  upstream: {}".format(tracking))
    lines.append("  activity: {}".format(activity))
    return lines


def enabled_app_ids(apps_config) -> list:
    apps = apps_config.get("apps") if isinstance(apps_config, dict) else apps_config
    if not isinstance(apps, list):
        return []
    ids = []
    for app in apps:
        if isinstance(app, dict) and app.get("enabled") and app.get("id"):
            ids.append(str(app["id"]))
    return ids


def main() -> int:
    repos_path, repos_config = load_json("repos.json", {"repos": []})
    apps_path, apps_config = load_json("apps.json", {"apps": []})
    repos = repos_config.get("repos") if isinstance(repos_config, dict) else []
    if not isinstance(repos, list):
        repos = []

    hours = int(os.environ.get("LOOKBACK_HOURS", repos_config.get("lookbackHours", 72)))
    now = datetime.now(timezone.utc).astimezone()
    lookback = now - timedelta(hours=hours)
    enabled = enabled_app_ids(apps_config)

    print(
        "# catch-me-up scan  {}  lookback={}h".format(
            now.isoformat(timespec="minutes"), hours
        )
    )
    print("repos.json: {}".format(repos_path or "(missing)"))
    print("apps.json: {}".format(apps_path or "(missing)"))
    print("apps enabled: {}".format(", ".join(enabled) if enabled else "(none)"))
    print()

    if not repos:
        print("NEED_SETUP: repos.json has no checkouts. Ask the user which repos to track.")
        print("Write answers to ~/.config/catch-me-up/ so the shipped skill stays reusable.")
        return 2

    for repo in repos:
        if not isinstance(repo, dict) or not repo.get("path"):
            print("## (invalid repo entry)")
            print("error: each repo needs name and path")
            print()
            continue
        name = repo.get("name") or Path(repo["path"]).name
        path = expand(repo["path"])
        kind = repo.get("kind", "work")
        print("## {}  {}  {}".format(name, kind, path))
        if not Path(path).exists():
            print("missing: checkout not found")
            print()
            continue
        print("remote: {}".format(remote_url(path)))
        stashes = stash_count(path)
        if stashes:
            print("stash: {} (repo-wide)".format(stashes))
        for tree in parse_worktrees(path):
            print("\n".join(inspect_worktree(tree, lookback)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
