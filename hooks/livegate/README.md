# LiveGate

LiveGate is a global Cursor hook that stops agents from starting duplicate
development servers. It keeps one current-state file per workspace containing
only live servers and failed starts.

## Requirements

- Cursor with Agent hooks support
- Python 3.10 or newer
- Optional: `lsof` for recording listener PIDs on macOS/Linux

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

No command matcher is used: LiveGate cheaply classifies each shell command from
`recipes.json`, so adding a recipe does not require changing `hooks.json`.

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

Entries have only two states:

- `live`: port, URL, PID, and go-live time
- `failed`: port, failure time, and concise reason

Stopping a server removes its entry. There is no event history.

## Behavior

- If a configured port is live, LiveGate denies the start and instructs the agent to
  ask: `The server <server-name> is running, do you want me to restart it?`
- Otherwise it allows the command and probes readiness in the background.
- A successful probe records `live`.
- A failed command or readiness timeout records `failed` with a reason.
- A successful shutdown command removes the matching entry.

Cursor hook `permission: "ask"` is unreliable today, so LiveGate uses `deny`
for duplicate starts.

## Recipes

The included defaults cover common Vite-style development commands on port
5173 and Storybook on port 6006.

Edit `recipes.json` for another project or server. Each recipe defines an ID,
display name, default port, and start/exclude/stop regexes. The command's
`--port` or `-p` option overrides the default port.

## Validate

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## Uninstall

Remove the LiveGate entries from `~/.cursor/hooks.json`, then remove
`~/.cursor/hooks/livegate/`. Workspace `.livegate/` state can be deleted
manually; LiveGate never deletes the state file itself.
