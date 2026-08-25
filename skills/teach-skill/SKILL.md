---
name: teach-skill
description: Turn direct feedback from a skill run into a confirmed, versioned improvement to that skill.
disable-model-invocation: true
---

# Teach a skill

Improve a user-owned skill from direct feedback. First correct the current work. Then teach the skill separately.

Never convert ambiguous feedback into a permanent rule without confirming what was learned and its intended scope.

Requires the companion model-invoked skills `/version-skill` and `/self-check`. If either is unavailable, stop and ask the user to install the complete teach-skill suite; do not reproduce their workflows inline.

## Boundaries

- Change one explicitly identified skill.
- Treat the active skill as the only discoverable version.
- Use `/version-skill` for candidate preparation, history, promotion, comparison, and restore.
- Use `/self-check` to validate a candidate before promotion.
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

For **Apply only to this task**, acknowledge the scope and change no files.

For **Save as my preference**:

1. Ask where it applies: this skill, this project, or all projects.
2. If it applies only to this skill, reclassify it as a permanent skill lesson and return to the confirmation step.
3. Otherwise inspect the live agent instruction files and rules at that scope. Update the existing canonical home instead of duplicating the preference.
4. Use an always-loaded agent file for a short preference that should apply throughout the chosen scope. Use a conditional or path-scoped agent rule when it applies only in named situations or files.
5. If no suitable home exists, create the smallest agent file or rule supported by the host.
6. Show the exact wording and destination, then wait for confirmation before writing.
7. Report where it was saved. Leave the target skill unchanged.

## 4. Prepare a candidate

Invoke `/version-skill` to prepare a candidate for the target skill. It must:

- Snapshot an unversioned active skill as version 1.
- Put the candidate and history outside all `skills/` directories.
- Return the candidate path.
- Refuse to overwrite an existing candidate. Ask whether to continue or discard it.

Edit only the candidate. Make the smallest change that teaches the confirmed lesson:

- Put the rule where the target skill already owns that decision.
- Prefer tightening an existing sentence over adding a new section.
- Update supporting files when behavior lives there.
- Add the smallest regression example supported by the skill. If it has no eval system, include the example in the version metadata rather than inventing infrastructure.

## 5. Validate and activate

Invoke `/self-check` with the active skill, candidate, confirmed lesson, and regression example. Promote only when it returns **PASS**.

Then invoke `/version-skill` to promote the candidate with the confirmed lesson and regression example. Promotion must:

- Preserve the candidate as a new version.
- Replace only the active skill.
- Keep archived versions undiscoverable.
- Fail without changing the active skill if activation cannot complete safely.

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

Only when the user explicitly asks, invoke `/version-skill` to show history, compare named versions, or restore one. Restoring creates a new version; it never rewinds or deletes history. After restore, only the newly restored active copy belongs in the discoverable skill directory.
