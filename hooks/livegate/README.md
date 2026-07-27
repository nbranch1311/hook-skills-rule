---
status: active
verified: 2026-07-27
---

# LiveGate

LiveGate is a global Cursor hook that stops agents from starting duplicate
local servers. It learns which commands start and stop servers from local
listener evidence, then writes a Next-like lock entry with the URL, PID, port,
workspace, and command.

## Requirements

- Cursor with Agent hooks support
- Python 3.10 or newer
- Optional but recommended: `lsof` for listener discovery and PID lookup

## Install

1. Copy this complete folder to:

```text
~/.cursor/hooks/livegate/
```

1. Make the scripts executable:

```bash
chmod +x ~/.cursor/hooks/livegate/livegate.py ~/.cursor/hooks/livegate/livegate-run ~/.cursor/hooks/livegate/selfcheck.py
```

1. Merge the entries from `hooks.example.json` into
   `~/.cursor/hooks.json`. Preserve any hooks already present:

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

No command matcher is used. LiveGate runs for every shell command, but the fast
path is only seed regexes and small JSON reads.

1. Validate the installation:

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## How It Learns

LiveGate no longer uses `recipes.json`.

1. A command matches a small seed start heuristic, such as `dev`, `serve`,
   `storybook`, `vite`, `next`, `uvicorn`, or `flask`.
1. LiveGate allows it, snapshots current listening ports, and starts a
   background probe.
1. If a new localhost listener appears, LiveGate records the command
   fingerprint and port in `.livegate/learned.json`.
1. It writes the live lock in `.livegate/servers.json`.

Unknown non-seed commands are not gated until they are learned. Use
`livegate-run` once for those:

```bash
~/.cursor/hooks/livegate/livegate-run your-server-command --port 8080
```

## Workspace State

Each workspace gets an ignored directory:

```text
.livegate/
  learned.json
  servers.json
```

When LiveGate first creates it in a Git workspace, it adds:

```gitignore
/.livegate/
```

`servers.json` is keyed by port. Live entries include:

- `port`
- `url`
- `pid`
- `cwd`
- `command`
- `fingerprint`
- `since`

Failed entries keep a concise reason.

## Duplicate Starts

When a learned start command targets an already-live port, LiveGate returns
`permission: "deny"` with a Next-like message:

```text
Another server is already running.

- Local:  http://localhost:5173
- PID:    12345
- Dir:    /path/to/project
- Cmd:    pnpm dev

Run: kill 12345
Or restart: LIVEGATE_RESTART=1 pnpm dev
```

Cursor hook `permission: "ask"` is unreliable today, so LiveGate uses `deny`.

Multiple servers can be live at once on different ports, such as Vite on 5173
and Storybook on 6006. A duplicate Vite start denies the Vite lock only.

## Stopping Servers

LiveGate never kills a process by itself. The agent runs a normal stop command,
such as `kill <pid>` or `kill-port <port>`.

On successful shell completion, LiveGate clears matching locks and learns that
stop fingerprint. If a server dies outside Cursor, the next shell command
prunes stale locks when the port is closed or the PID is gone.

To intentionally restart:

```bash
LIVEGATE_RESTART=1 pnpm dev
```

## Manual Subagent Check

1. Ask one subagent to start a dev server and wait for localhost to respond.
1. Confirm `.livegate/learned.json` and `.livegate/servers.json` exist.
1. Ask another subagent to start the same server.
1. The second subagent's shell command should be denied with the Local, PID,
   Dir, and kill instructions.
1. Start Storybook while Vite is live to confirm different ports can coexist;
   a second Storybook start should be denied.

## Validate

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## Ceiling

- Unknown non-seed commands are ungated until learned or wrapped with
  `livegate-run`.
- Fingerprints match by normalized command equality, so variants like
  `pnpm --filter app dev` and `pnpm dev` learn separately.
- Two parallel first starts can race before either binds. Sequential agent
  starts are covered once the first lock is written.

## Uninstall

Remove the LiveGate entries from `~/.cursor/hooks.json`, then remove
`~/.cursor/hooks/livegate/`. Workspace `.livegate/` state can be deleted
manually; LiveGate never deletes the state file itself.
