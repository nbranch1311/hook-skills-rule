---
status: active
verified: 2026-07-27
---

# auto-format-prettier

`afterFileEdit` hook that runs Prettier on the file Cursor just edited (Agent + Tab). Best-effort: never blocks the agent.

## Install

1. Copy `format.sh` to `.cursor/hooks/auto-format-prettier.sh` and `chmod +x` it.
2. Merge `hooks.example.json` into your project's `.cursor/hooks.json` (or `~/.cursor/hooks.json` for user-wide).

## Requires

- [jq](https://jqlang.org/) — `brew install jq`
- Node + Prettier via `./node_modules/.bin/prettier`, or `npx prettier`

Missing tools print a one-line stderr hint and exit 0.

## Check

```bash
./hooks/auto-format-prettier/selfcheck.sh
```
