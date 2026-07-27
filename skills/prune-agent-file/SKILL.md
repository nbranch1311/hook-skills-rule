---
name: prune-agent-file
description: >-
  Trim an agent instruction file (AGENTS.md, CLAUDE.md, or similar) to remove
  anything that is no longer needed, contradictory, duplicate information, or
  unnecessary bloat impacting its effectiveness. Use when the user asks to trim,
  prune, tighten, dedupe, or clean up an AGENTS.md / CLAUDE.md / agent guidance
  file.
disable-model-invocation: true
---

# Trim Agent File

Update the target agent instruction file (AGENTS.md, CLAUDE.md, .cursorrules, GEMINI.md, or any similar agent guidance file) to remove anything that is no longer needed, contradictory, duplicate information, or unnecessary bloat impacting its effectiveness.

## Target file

Only run this skill on a file the user explicitly named. If the user did not name a specific file (or files), do not guess or pick one yourself — ask which file(s) to trim before doing anything else.

## Goal

The file competes for context window space on every request. Cut ruthlessly, but never delete a rule that is unique and still true. Fewer, non-redundant, accurate instructions are more effective than a long file.

## Process

1. **Read the whole file first.** Understand its structure before cutting.
2. **Verify references against reality.** For every path, file, command, package, or tool the file names, check it still exists (Glob/Read). Flag:
   - Stale markers like `(when added)`, `(coming soon)`, `(TODO)` for things that now exist or were removed.
   - References to deleted/renamed files, dead links, obsolete commands.
   - Note: gitignored paths (e.g. `.cursor/`, `.claude/`, local config dirs) may be invisible to search tools — do not assume they are missing just because a search returns nothing.
3. **Find contradictions.** Two statements that disagree (e.g. one section lists a file as a live reference, another marks it "when added"). Resolve to the true state.
4. **Find duplicates.** The same rule stated in multiple sections (often verbatim). Keep it in the single most relevant place; delete the copies. Watch for near-duplicates that only differ in wording.
5. **Cut bloat.** Generic filler ("follow existing code style"), redundant headings, restating what the agent already knows, and empty ceremony.
6. **Preserve every unique rule.** Before deleting a bullet, confirm the same information survives elsewhere. Unique specifics (exact commands, hard constraints, one-way dependency flows, "avoid X" rules) must stay somewhere. When merging, fold unique nuggets into the surviving bullet rather than dropping them.
7. **Keep the existing structure and tone.** Do not rewrite from scratch; edit in place with minimal, surgical changes.

## Sync sibling files

- Some repos auto-sync agent files one from the other (via a hook, symlink, or script). If editing one file makes the other match automatically, do not fight it — verify with for example `diff AGENTS.md CLAUDE.md`.
- If they are separate and unsynced then leave it that way.

## Verify

- The file is shorter and every remaining line earns its place.
- No unique rule was lost (spot-check against the pre-trim version).
- No broken references remain.
- Sibling instruction files are identical (or intentionally divergent).
