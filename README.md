---
status: active
verified: 2026-09-16
---

# Hooks, Skills, Rule

Agents are expensive to train and already smart, but they still need direction,
rules, and boundaries. Rules create order, hooks make agents predictable, and
skills make what they can do repeatable.

This repository is a collection of reusable AI-agent tooling, organized by
type.

## Install

The consumer path is GitHub plus the Skills CLI. That is the only supported
install.

**Toolkit** (every skill in this repo):

```bash
npx skills add nbranch1311/hook-skills-rule -g -y
```

**Single skill:**

```bash
npx skills add nbranch1311/hook-skills-rule@agent-factory -g -y
```

Swap `agent-factory` for any skill name below. Same thing with a flag:
`--skill agent-factory`.

**Teach-skill suite** (these three work together):

```bash
npx skills add nbranch1311/hook-skills-rule --skill teach-skill --skill version-skill --skill self-check -g -y
```

Preview without installing:

```bash
npx skills add nbranch1311/hook-skills-rule --list
```

Update later with `npx skills update -g`. `-g` is user-global. Omit it to
install into the current project only.

Do not copy or symlink skill folders into `~/.claude/skills`,
`~/.cursor/skills`, or other agent directories. The CLI owns that mapping.

## Hooks

- [auto-format-prettier](hooks/auto-format-prettier/) — `afterFileEdit` hook
  that runs Prettier on the edited file (best-effort, never blocks).

## Skills

Teach-skill suite — install all three:

- [teach-skill](skills/teach-skill/) — turns confirmed user feedback into a
  versioned improvement to the active skill.
- [version-skill](skills/version-skill/) — model-invoked version preparation,
  promotion, history, comparison, and append-only restore for `teach-skill`.
- [self-check](skills/self-check/) — model-invoked promotion gate for candidate
  skill changes.

Other skills:

- [agent-factory](skills/agent-factory/) — interviews for a project, global, or
  named agent file, then creates or patches it.
- [catch-me-up](skills/catch-me-up/) — morning briefing of tracked repos,
  worktrees, and enabled apps (Jira, calendars, mail, and similar).
- [demo-mode](skills/demo-mode/) — minimizes latency and narration during a
  live demonstration.
- [prune-agent-file](skills/prune-agent-file/) — removes stale, duplicate, and
  contradictory agent instructions without dropping unique rules.

## Authoring

While editing this checkout, re-add from the local path after a skill change.
Live symlinks into agent skill folders are not the consumer path; decide that
separately if you want a faster author loop.

```bash
npx skills add /path/to/hook-skills-rule --skill agent-factory -g -y
```

## License

[MIT](LICENSE)
