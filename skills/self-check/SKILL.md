---
name: self-check
description: Validate a candidate Agent Skill against a confirmed lesson, regression example, and active version before promotion. Use when teach-skill proposes a permanent skill improvement.
---

# Self-check a skill change

Validate one active skill and its candidate using the host's native read and comparison tools. Do not modify either copy.

## Inputs

Require:

- Active skill path
- Candidate path
- Confirmed lesson and scope
- Regression example
- Whether the user explicitly approved a charter change

If any input is missing, return **FAIL** with the missing input. Do not guess.

## Checks

### Structure

- `SKILL.md` exists and has valid frontmatter.
- The frontmatter name still identifies the same skill.
- Local references resolve from the candidate's future active location.
- All active supporting files are preserved unless intentionally changed.
- No version archive or candidate is nested under a discoverable `skills/` directory.

### Lesson

- The candidate expresses the confirmed lesson as a reusable condition and action.
- The instruction is specific enough to behave consistently next time.
- The change addresses the observed cause, not only the example's wording.
- The regression example would fail under the old guidance and pass under the candidate.

### Preservation

- Purpose, authority, safety boundaries, human gates, and explicit prohibitions are unchanged.
- A charter element changes only when the user explicitly approved a charter change.
- The candidate does not contradict another live instruction.

### Scope

- The diff is the smallest change that teaches the lesson.
- There is no unrelated cleanup, reformatting, renaming, or speculative guidance.
- Session-specific facts and sensitive source material were not copied into the permanent skill or version metadata.

## Result

Return exactly one verdict:

```markdown
## Skill self-check: PASS

- Structure: pass
- Lesson: pass — <evidence>
- Preservation: pass — <evidence>
- Scope: pass — <evidence>
- Regression: pass — <old behavior → candidate behavior>
```

or:

```markdown
## Skill self-check: FAIL

- <failed check>: <specific reason>
- Required correction: <smallest candidate change>
```

Do not promote on **FAIL**. After correcting the candidate, run the complete self-check again.
