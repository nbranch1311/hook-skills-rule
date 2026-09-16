# App recipes

Only run recipes for apps with `"enabled": true` in `apps.json`. Discover tools at runtime. If a server `needsAuth`, authenticate once and retry. If it is missing, say so and skip — do not invent events, tickets, or mail.

Cap each source. Morning read, not a dump.

## jira

Assigned, unresolved:

```
assignee = currentUser() AND resolution is EMPTY ORDER BY updated DESC
```

Watching, unresolved, not assigned to me:

```
watcher = currentUser() AND assignee != currentUser() AND resolution is EMPTY ORDER BY updated DESC
```

Cap 15 assigned + 10 watching. One line each: key, summary, status, last updated. Flag blocked, impediment, in-progress with no recent update, or due today/overdue.

## confluence

Pages I edited, was mentioned on, or watch that changed inside the lookback. Title, space, last updated. Cap 8. No page-body paste.

## azure-devops

Work items assigned to me that are not closed/done (WIQL or `az boards`). Cap 15. One line: id, title, state.

Pull requests for Azure remotes already come from the repo scan — do not double-list them here. Use this app for boards/work items.

## github

Org-wide, beyond tracked remotes:

```bash
gh pr list --author @me --state open
gh pr list --search "review-requested:@me" --state open
```

Skip PRs already listed from a tracked checkout. Flag failing checks or changes-requested.

## google-calendar

Today's events (local timezone): time, title, conflict if overlapping. Next 3 hours first. Cap 12. No attendee dumps.

## outlook

Today's calendar and flagged / @mentioned mail. Subject, folder, time. Cap 8. Never paste bodies or full threads.

## linear

Assigned, unfinished issues. Identifier, title, state, last updated. Cap 15.

## slack

Unread DMs and @mentions inside the lookback. Channel/DM name, one-line preview. Cap 8. No private-channel fishing if the MCP forbids it.

## Adding an app

Add an object to `apps.json`:

```json
{
  "id": "short-id",
  "label": "Human name",
  "enabled": true,
  "kind": "mcp",
  "discover": ["keyword"]
}
```

Use `"kind": "cli"` and `"cli": "binary"` when the check is a local command. Document the query in this file.
