---
name: catch-me-up
description: Morning briefing across the user's tracked git repos, worktrees, and enabled apps (Jira, Confluence, calendars, mail, and similar). Reports where work left off, what is still pending, and what needs immediate attention. Use when the user says catch me up, catch-me-up, morning briefing, where did I leave off, start of day, or coming in for work.
---

# Catch me up

Morning status briefing. Read-only: do not commit, push, edit tickets, send mail, or start implementing.

This skill is generic. Tracked checkouts live in `repos.json`. Extra apps/MCPs live in `apps.json`. Prefer the user's config dir over the copies shipped with the skill (see Config).

## Config

Resolve files in this order (first hit wins per file):

1. `$CATCH_ME_UP_HOME/`
2. `~/.config/catch-me-up/`
3. this skill directory

Execute, do not read:

```bash
python3 <this-skill-dir>/scripts/scan-repos.py
```

Needs `python3` and `git`. Override the lookback with `LOOKBACK_HOURS` (default 72). Paths in `repos.json` may use `~`.

### Empty `repos.json` → ask

If the scan prints `NEED_SETUP` or `repos` is missing/`[]`, **stop the briefing and ask**. Do not invent a repo list. Do not scan the home directory.

Ask:

1. Which git checkouts to track — name, absolute or `~/` path, and `work` / `personal` if they care. If they name a parent folder, list git repos in that folder and let them pick. Wait for the list.
2. Which apps to enable from `apps.json` (multi-select is fine). They can add an id that is not in the catalog. They can choose none.

Then write `repos.json` and `apps.json` to `~/.config/catch-me-up/` (create the dir). Keep the skill-directory templates empty/catalog-default so installs stay reusable. Continue the briefing in the same turn once config exists.

Later runs with a non-empty `repos` list and all apps disabled are git-only. Do not nag.

## Workflow

```
Catch-me-up:
- [ ] Scan repos + worktrees (or first-run setup)
- [ ] Open PRs/MRs from tracked remotes
- [ ] Each enabled app in apps.json
- [ ] Brief recent transcripts (optional)
- [ ] Write the briefing
```

### 1. Repos + worktrees

Use `activity:` tags from the script:

- `HOT (dirty)` / `HOT (unpushed)` → pending; immediate attention if the WIP looks stale or risky
- `RECENT` → where I left off
- `quiet` → collapse under Quiet
- `parked` → omit unless dirty (clean detached experiment/control copies)

Untracked-only trees whose files sit in `.unlazy/`, `node_modules`, `.scratch`, `dist` are **not** WIP. Do not treat a repo-wide `stash:` count as today's work.

"Where I left off" is the newest primary checkout (or extra worktree) with `RECENT`/`HOT` activity inside the lookback. Older HOT extra worktrees are leftover WIP under Still pending, not this morning's thread.

If a listed path is missing, say so.

### 2. Open PRs / MRs

From each **unique** `remote:` (once per host/repo, not per worktree):

**github.com** — `gh pr list --author @me --state open` and `gh pr list --search "review-requested:@me" --state open`

**dev.azure.com** / **visualstudio.com**:

```bash
creator=$(az account show --query user.name -o tsv)
az repos pr list --detect true --status active --creator "$creator" -o table
az repos pr list --detect true --status active --reviewer "$creator" -o table
```

**gitlab.com** — `glab mr list --assignee=@me` if `glab` exists.

Skip a host when its CLI is missing, and say so. Failing checks or waiting-on-me reviews → immediate attention.

### 3. Enabled apps

Read the enabled list from the scan header (`apps enabled:`). For each id, follow [apps.md](apps.md). Discover MCP tools with `GetDynamicTools` using that app's `discover` keywords. Never assume a server is installed.

### 4. Recent transcripts (optional, cheap)

If `~/.cursor/projects/*/agent-transcripts/*.jsonl` (or the host's equivalent) were modified inside the lookback, use the newest 1–3 only to name the last topic. Do not dump the chat.

## Briefing

Lead with today's date. Keep it a morning read. Group work vs personal when both have signal.

```markdown
# Catch me up — {weekday, date}

## Immediate attention

Anything that should have my immediate attention.
Uncommitted work at risk of loss, unpushed branches, review-requested PRs,
failing checks, blocked/stale in-progress tickets, overlapping meetings.
Or: Nothing urgent.

## Where I left off

Update me on where I left off.
Most recent repo/worktree/branch, last commit, dirty files, last topic.

## Still pending

What is still pending.
Open branches, unpushed commits, open PRs, in-progress tickets. Skip quiet repos.

## {App label}

One section per enabled app. Quick status. Omit the section if that app had nothing.

## Quiet

One line per tracked repo with no signal. Omit the section if none.
```

## Guardrails

- Evidence only: git scan, CLIs, enabled app MCPs, recent transcripts. No guessed "probably working on X".
- Do not start the day's work unless asked after the briefing.
- Skip clean detached experiment/control/treatment worktrees.
- Skip empty sibling folders that are not git checkouts.
- Never print secrets, tokens, `.env*` contents, or full email/ticket bodies.
