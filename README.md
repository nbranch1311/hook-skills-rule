---
status: active
verified: 2026-08-25
---

# Hooks, Skills, Rule

Agents are expensive to train and already smart, but they still need direction,
rules, and boundaries. Rules create order, hooks make agents predictable, and
skills make what they can do repeatable.

This repository is a collection of reusable AI-agent tooling, organized by
type.

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

- [demo-mode](skills/demo-mode/) — minimizes latency and narration during a
  live demonstration.
- [prune-agent-file](skills/prune-agent-file/) — removes stale, duplicate, and
  contradictory agent instructions without dropping unique rules.

## License

[MIT](LICENSE)
