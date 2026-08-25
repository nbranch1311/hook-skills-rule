---
name: version-skill
description: Version, promote, compare, or restore a user-owned Agent Skill using the host's native file tools. Use when teach-skill needs candidate management or when the user asks for skill history or rollback.
---

# Version a skill

Manage local skill versions without scripts, language runtimes, Git, branches, or pull requests.

## Storage

Given an active skill under a directory named `skills/`, keep state beside that directory:

```text
<skills-parent>/.skill-versions/<skill-name>/
├── HISTORY.md
├── candidate/
└── versions/
    ├── 0001/
    └── 0002/
```

Create `<skills-parent>/.skill-versions/.gitignore` containing `*` so history stays local. Never put candidates or archived `SKILL.md` files inside any `skills/` directory.

Use the target `SKILL.md` frontmatter name as `<skill-name>`. If no ancestor is named `skills`, ask the user where local history should live.

Use native file, directory, and comparison tools supplied by the host. Do not create or execute helper scripts. If the host cannot copy the complete skill folder safely, stop and explain the missing capability.

## Rules

- Snapshot the complete skill folder, including references, assets, and existing scripts.
- Preserve permissions and symlinks when the host supports them; never silently produce a lossy snapshot.
- `HISTORY.md` is metadata. Do not read archived skill contents unless the requested operation is compare or restore.
- Keep one candidate per skill. Never overwrite or discard it implicitly.
- Only the active skill remains in the discoverable `skills/` location.
- Versions are append-only. Never modify or delete an existing numbered version.

## Prepare

1. Resolve the active skill and state paths.
2. If no history exists:
   - Copy the active folder to `versions/0001/`.
   - Create `HISTORY.md` with `Active: 1` and an “Initial imported version” entry.
3. If history exists, verify the active folder still matches the recorded active version. If it was edited outside this workflow, preserve it as the next version labelled “Externally modified baseline” before continuing.
4. Refuse when `candidate/` already exists; ask whether to continue it or discard it.
5. Copy the complete active folder to `candidate/`.
6. Return the active version and candidate path.

## Promote

Require a **PASS** from `/self-check`, the confirmed lesson, and the regression example.

1. Choose the next integer version.
2. Copy the complete candidate to that numbered version.
3. Prepare a complete replacement beside the active folder.
4. Move the active folder to a temporary backup, move the replacement into the active path, and immediately verify `SKILL.md` plus every candidate file.
5. On failure, restore the backup and leave `HISTORY.md` unchanged.
6. On success, remove the backup and candidate.
7. Append the version, timestamp, lesson, and regression example to `HISTORY.md`; set `Active` to the new version.

Return the previous version, active version, lesson, and validation result.

## History

When explicitly requested, read `HISTORY.md` only. Show version, timestamp, and lesson. Do not inspect archived contents.

## Compare

When explicitly requested, compare exactly the two named version folders. Show a concise file and content diff without activating either version.

## Restore

When explicitly requested:

1. Copy the named archived version to `candidate/`.
2. Show which version will be restored and ask for confirmation if the request was ambiguous.
3. Promote it as the next version with `Restored from: <version>` in `HISTORY.md`.

Never set `Active` backward. A restore of version 2 after version 5 becomes version 6.
