---
name: teach-skill
description: Turn direct user feedback from a skill run into a confirmed, durable improvement to that skill. Use only when the user explicitly invokes /teach-skill to teach, inspect history, compare versions, or restore a prior version.
disable-model-invocation: true
---

# Teach a skill

Improve a user-owned skill from direct feedback. First correct the current work. Then teach the skill separately.

Never convert ambiguous feedback into a permanent rule without confirming what was learned and its intended scope.

## Boundaries

- Change one explicitly identified skill.
- Treat the active skill as the only discoverable version.
- Keep history outside every `skills/` directory in the version store created by `scripts/version_skill.py`.
- Do not read historical skill contents unless the user asks to compare or restore them.
- Never weaken the target skill's purpose, authority boundary, safety rule, or explicit prohibition as an incidental lesson.
- Do not create branches, commits, or pull requests.

## 1. Identify the teaching moment

Use the user's correction and the surrounding skill run. If no exact target skill is evident, ask which skill to teach. If no concrete feedback is evident, ask what should work differently next time.

Apply the correction to the current task before starting this workflow when work is still in progress.

## 2. Classify the feedback

Choose one:

- **Current task only** — specific to this case; do not persist.
- **User preference** — personal style or choice that may span skills; do not put it in this skill unless the user chooses that scope.
- **Permanent skill lesson** — reusable behavior for this target skill.
- **Charter change** — changes the skill's purpose, authority, safety boundary, or prohibited actions; label it explicitly and require confirmation of that larger scope.

When classification or scope is uncertain, ask. Do not infer permanence from words such as “wrong” or “I don't like this.”

## 3. Generalize the lesson

Read the active target skill and only its live supporting files. Find the smallest shared cause of the observed behavior.

Present:

```markdown
## Teach <skill-name>

**Observed problem**
<what happened in this run>

**Proposed lesson**
<when condition X occurs, do Y; never do Z>

**Applies when**
<trigger and boundaries>

**Scope**
Current task | User preference | Permanent rule for <skill> | Charter change

**Current behavior**
<what the active skill says or omits>

**Proposed change**
<smallest files/sections to change>

**Regression check**
<example that would fail before and pass after>
```

Check for contradictions with the active skill. If a conflict or vague term remains, show it and ask a focused question.

Offer exactly:

1. Apply permanently
2. Edit the lesson
3. Apply only to this task
4. Save as my preference
5. Cancel

Do not modify the active skill before the user selects **Apply permanently** or explicitly confirms a **Charter change**.

For **Apply only to this task**, acknowledge the scope and change no files. For **Save as my preference**, use the host's user-scoped memory or instruction mechanism, report where it was saved, and leave the target skill unchanged.

## 4. Prepare a candidate

Resolve the target skill directory and this skill's `scripts/version_skill.py`.

Run:

```bash
python3 <teach-skill-dir>/scripts/version_skill.py prepare <target-skill-dir>
```

The command snapshots an unversioned active skill as version 1 and returns a candidate path outside `skills/`. If a candidate already exists, stop and ask whether to continue that lesson or discard it. Never delete it implicitly.

If the user chooses discard:

```bash
python3 <teach-skill-dir>/scripts/version_skill.py discard <target-skill-dir>
```

Edit only the candidate. Make the smallest change that teaches the confirmed lesson:

- Put the rule where the target skill already owns that decision.
- Prefer tightening an existing sentence over adding a new section.
- Update supporting files when behavior lives there.
- Add the smallest regression example supported by the skill. If it has no eval system, include the example in the version metadata rather than inventing infrastructure.

## 5. Validate and activate

Validate the candidate:

```bash
python3 <teach-skill-dir>/scripts/version_skill.py validate <candidate-dir>
python3 <teach-skill-dir>/scripts/version_skill.py diff <target-skill-dir>
```

Confirm that:

- The candidate expresses the agreed lesson.
- The regression example now follows the intended behavior.
- Existing purpose, boundaries, gates, and prohibitions remain intact.
- No unrelated cleanup is included.

Write the confirmed lesson and regression example to a temporary UTF-8 text file, then promote:

```bash
python3 <teach-skill-dir>/scripts/version_skill.py promote <target-skill-dir> \
  --lesson-file <lesson-file> \
  --example-file <example-file>
```

Promotion must fail without changing the active skill when validation fails.

Report:

```text
Updated <skill-name>
Version: <old> → <new>
Lesson: <one sentence>
Validation: passed
Active now: version <new>
```

State that the new version applies to future invocations. Follow the confirmed lesson for the rest of the current conversation, but recommend a new conversation for a clean test because an older skill version may already be in context.

## History, comparison, and restore

Only do these when the user explicitly requests them.

```bash
python3 <teach-skill-dir>/scripts/version_skill.py history <target-skill-dir>
python3 <teach-skill-dir>/scripts/version_skill.py compare <target-skill-dir> <version-a> <version-b>
python3 <teach-skill-dir>/scripts/version_skill.py restore <target-skill-dir> <version> \
  --lesson "Restore version <version>"
```

Restoring creates a new version; it never rewinds or deletes history. After restore, only the newly restored active copy belongs in the discoverable skill directory.
