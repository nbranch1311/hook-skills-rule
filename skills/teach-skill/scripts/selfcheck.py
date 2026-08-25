#!/usr/bin/env python3
"""Exercise teach-skill versioning in a temporary project."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("version_skill.py")
CURSOR_DIR = ".cursor"
STATE_DIR = ".teach-skill"
SKILL_FILE = "SKILL.md"
ORIGINAL_BEHAVIOR = "original behavior"
LEARNED_BEHAVIOR = "learned behavior"
INITIAL = """---
name: sample
description: Sample skill for the self-check.
disable-model-invocation: true
---

# Sample

Use the original behavior.
"""


def run(*arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr or result.stdout
    return result


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        skill = root / CURSOR_DIR / "skills" / "sample"
        skill.mkdir(parents=True)
        (skill / SKILL_FILE).write_text(INITIAL, encoding="utf-8")

        prepared = json.loads(run("prepare", str(skill)).stdout)
        candidate = Path(prepared["candidate"])
        assert candidate == root / CURSOR_DIR / STATE_DIR / "sample" / "candidate"
        assert (root / CURSOR_DIR / STATE_DIR / ".gitignore").read_text(
            encoding="utf-8"
        ) == "*\n"
        assert not any(
            path.name == SKILL_FILE for path in skill.parent.glob("**/versions/**")
        )

        changed = INITIAL.replace(ORIGINAL_BEHAVIOR, LEARNED_BEHAVIOR)
        (candidate / SKILL_FILE).write_text(changed, encoding="utf-8")
        lesson = root / "lesson.txt"
        example = root / "example.txt"
        lesson.write_text("Use learned behavior in this situation.", encoding="utf-8")
        example.write_text("Original fails; learned behavior passes.", encoding="utf-8")

        promoted = json.loads(
            run(
                "promote",
                str(skill),
                "--lesson-file",
                str(lesson),
                "--example-file",
                str(example),
            ).stdout
        )
        assert promoted["activeVersion"] == 2
        assert LEARNED_BEHAVIOR in (skill / SKILL_FILE).read_text(encoding="utf-8")

        history = json.loads(run("history", str(skill)).stdout)
        assert history["activeVersion"] == 2
        assert [entry["version"] for entry in history["versions"]] == [1, 2]

        restored = json.loads(
            run(
                "restore",
                str(skill),
                "1",
                "--lesson",
                "Restore the original behavior.",
            ).stdout
        )
        assert restored["activeVersion"] == 3
        assert ORIGINAL_BEHAVIOR in (skill / SKILL_FILE).read_text(encoding="utf-8")

        comparison = run("compare", str(skill), "1", "2").stdout
        assert ORIGINAL_BEHAVIOR in comparison
        assert LEARNED_BEHAVIOR in comparison

        run("prepare", str(skill))
        run("prepare", str(skill), expected=1)
        run("discard", str(skill))
        assert not (root / CURSOR_DIR / STATE_DIR / "sample" / "candidate").exists()

    print("teach-skill self-check passed")


if __name__ == "__main__":
    main()
