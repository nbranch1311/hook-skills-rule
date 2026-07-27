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
        "command": "~/.cursor/hooks/livegate/livegate.py",
        "matcher": "Shell",
        "failClosed": false,
        "timeout": 5
      }
    ],
    "beforeShellExecution": [
      {
        "command": "~/.cursor/hooks/livegate/livegate.py",
        "failClosed": false,
        "timeout": 5
      }
    ]
  }
}
```

No command matcher is used: LiveGate reads optional application mappings from
the current workspace, resolves common JavaScript package scripts, and learns
strongly attributed custom commands.

The module has no Python package dependencies. It uses only the standard
library and the detected platform listener tool.

4. Validate the installation:

```bash
python3 -B ~/.cursor/hooks/livegate/selfcheck.py
```

## File layout

- `livegate.py`: executable hook entrypoint.
- `livegate_hooks.py`: Cursor before/post hook policy and feedback.
- `livegate_lifecycle.py`: observation, revalidation, fallback promotion, and failure notices.
- `livegate_identity.py`: workspace mappings, JavaScript package inference, and learned identities.
- `livegate_platform.py`: macOS/Linux process, listener, endpoint, and attribution helpers.
- `livegate_state.py`: v3 state schema, locking, writes, and attempt helpers.
- `livegate_commands.py`: command normalization, redaction, hashing, and approval tokens.

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
Version-1 state and legacy recipe semantics are unsupported.

## Domain model

- **Logical Application**: stable deduplication identity, independent of port.
- **Server Instance**: one verified agent-origin process and its endpoints.
- **Launch Attempt**: one agent shell command and its separate shell outcome.
- **Launch Group**: one command that starts multiple applications or endpoints.
- **Agent Provenance**: PID, process-start identity, endpoint, and fingerprint
  evidence tying an instance to an observed agent launch.

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
- Commands mapped to multiple applications are tracked as launch groups.
  Ambiguous commands advertising multiple endpoints receive a learned fallback
  group identity. Partially live groups become `degraded` and remain protected
  from automatic relaunch.
- Live instances are revalidated after agent events using their PID, process
  start identity, and endpoint. Stale instances are removed.
- Unknown commands are allowed.
- A failed command or startup timeout releases its reservation and stores a
  short redacted reason. Shell outcome remains separate from server health.

Cursor hook `permission: "ask"` is unreliable today, so LiveGate uses `deny`
for duplicate starts.

Routine successful starts and health checks are quiet. Duplicate decisions,
failed launches, and explicit exception flows return concise feedback. Internal
errors, unavailable inspection tools, lock contention, unsupported platforms,
and inspection timeouts fail open; only a verified duplicate is denied.
Stored commands and diagnostics redact environment values, credential flags,
Bearer values, and sensitive URL queries; advertised endpoint queries are not
persisted.

LiveGate observes agent Shell events only. It ignores commands and processes
started manually by users. It never launches, stops, restarts, supervises, or
continuously polls application servers. Startup waiting is bounded and runs
only in a detached observer process.

macOS and Linux are supported initially. Windows and other unsupported
platforms fail open. Ordinary synchronous events target less than 200 ms and
make no model or external network calls.

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
    },
    {
      "id": "stack-ui",
      "commands": ["pnpm stack"],
      "endpointIndex": 0
    },
    {
      "id": "stack-api",
      "commands": ["pnpm stack"],
      "endpointIndex": 1
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
