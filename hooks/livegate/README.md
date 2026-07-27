---
status: active
verified: 2026-07-27
---

# LiveGate

LiveGate is a global Cursor hook that stops agents from starting duplicate
instances of the same configured logical application. Ports are discovered
runtime details, not application identities.

## Requirements

- Cursor with Agent hooks support
- Python 3.10 or newer
- macOS with `lsof`, or Linux with `ss`

## Install

1. Copy this complete folder to:

```text
~/.cursor/hooks/livegate/
```

2. Make the scripts executable:

```bash
chmod +x ~/.cursor/hooks/livegate/livegate.py ~/.cursor/hooks/livegate/selfcheck.py
```

3. Merge the entries from `hooks.example.json` into
   `~/.cursor/hooks.json`. Preserve any hooks already present. The resulting
   LiveGate entries are:

```json
{
  "version": 1,
  "hooks": {
    "postToolUse": [
      {
        "command": "./hooks/livegate/livegate.py",
        "matcher": "Shell",
        "failClosed": false,
        "timeout": 5
      }
    ],
    "beforeShellExecution": [
      {
        "command": "./hooks/livegate/livegate.py",
        "failClosed": true,
        "timeout": 5
      }
    ]
  }
}
```

No command matcher is used: LiveGate reads optional application mappings from
the current workspace, resolves common JavaScript package scripts, and learns
strongly attributed custom commands.

4. Validate the installation:

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## Current state

Each workspace gets one ignored file:

```text
.livegate/
  servers.json
```

When LiveGate first creates it in a Git workspace, it adds:

```gitignore
/.livegate/
```

The version 3 state contains:

- logical applications and all of their `starting` or `live` instances
- the latest 50 redacted launch attempts

Version 3 intentionally ignores older state because the instance model changed.

## Behavior

- Before a mapped command runs, LiveGate atomically reserves its logical
  application as `starting`.
- Another mapped command for that application is denied while it is starting.
- A loopback HTTP(S) URL advertised by successful shell output is verified and
  attributed to the agent process before it is recorded as `live`.
- If no URL is advertised, a new attributed HTTP listener is used as fallback.
- A short-lived observer waits up to `startupTimeoutSeconds` (60 by default)
  without supervising or terminating the server.
- A verified live application denies later equivalent commands and reports its
  existing URL.
- Duplicate feedback offers reuse, a targeted agent-managed restart, or an
  explicitly approved second instance. Second instances require the one-use
  five-minute token returned by the denial; LiveGate never stops or starts the
  process itself.
- Every approved instance remains listed under the same logical application,
  and stopping one does not hide surviving instances.
- Live instances are revalidated after agent events using their PID, process
  start identity, and endpoint. Stale instances are removed.
- Unknown commands are allowed.
- A failed command or startup timeout releases its reservation and stores a
  short redacted reason. Shell outcome remains separate from server health.

Cursor hook `permission: "ask"` is unreliable today, so LiveGate uses `deny`
for duplicate starts.

## Workspace mappings

Add an optional committed `livegate.json` at the workspace root:

```json
{
  "version": 1,
  "startupTimeoutSeconds": 60,
  "applications": [
    {
      "id": "docs",
      "name": "Documentation",
      "commands": ["pnpm dev", "pnpm run docs"],
      "packageScripts": ["@scope/docs:dev"]
    }
  ]
}
```

Commands are normalized exact aliases. Package-script identities use
`<package>:<script>`; `.` identifies the workspace root. No port is configured.
Mappings override automatic inference.

Without a mapping, LiveGate resolves npm, pnpm, yarn, and bun scripts from
`package.json` metadata and distinguishes Vite from Storybook by package and
server family. Unknown commands remain allowed; when one advertises a strongly
attributed loopback server, its normalized command and working directory are
learned for later duplicate checks. An opaque alias can therefore bypass
deduplication once before it is learned.

## Validate

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## Uninstall

Remove the LiveGate entries from `~/.cursor/hooks.json`, then remove
`~/.cursor/hooks/livegate/`. Workspace `.livegate/` state can be deleted
manually; LiveGate never deletes the state file itself.
