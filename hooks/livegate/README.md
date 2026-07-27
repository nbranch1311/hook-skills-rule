# LiveGate

LiveGate is a global Cursor hook that stops agents from starting duplicate
instances of the same configured logical application. Ports are discovered
runtime details, not application identities.

## Requirements

- Cursor with Agent hooks support
- Python 3.10 or newer

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
the current workspace.

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

The version 2 state contains:

- logical applications that are `starting` or `live`
- the latest 50 redacted launch attempts

## Behavior

- Before a mapped command runs, LiveGate atomically reserves its logical
  application as `starting`.
- Another mapped command for that application is denied while it is starting.
- A loopback HTTP(S) URL advertised by successful shell output is verified and
  recorded as `live`.
- A verified live application denies later equivalent commands and reports its
  existing URL.
- Unknown commands are allowed.
- A failed mapped shell command releases its reservation.

Cursor hook `permission: "ask"` is unreliable today, so LiveGate uses `deny`
for duplicate starts.

## Workspace mappings

Add an optional committed `livegate.json` at the workspace root:

```json
{
  "version": 1,
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

## Validate

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## Uninstall

Remove the LiveGate entries from `~/.cursor/hooks.json`, then remove
`~/.cursor/hooks/livegate/`. Workspace `.livegate/` state can be deleted
manually; LiveGate never deletes the state file itself.
