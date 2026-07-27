---
name: demo-mode
description: Live-demo mode — completes real requests with minimum latency and minimal narration while an audience watches. Only for explicit manual invocation via /demo; never auto-apply.
disable-model-invocation: true
---

# Demo mode

The user is presenting live. Every second of agent time is dead air on stage. Speed is the deliverable; the work must still be real and correct, but take the shortest correct path.

Note: demos are typically held on a branch that starts with demo-\* (example: demo-mcp-presentation)

## Behavior

- Do exactly what was asked — nothing optional. No proactive improvements, no adjacent cleanup, no "while I'm here" work.
- Take the shortest correct path. Prefer one tool call over three. Batch independent calls in parallel.
- Skip todo lists, planning preambles, and progress narration. Act immediately.
- Skip broad exploration. Read only files the request directly names or requires. Do not survey the codebase for context you can live without.
- When the prompt names a specific tool, MCP server, file, or command, use it directly — no discovery, no verification detours.
- Skip validation beyond what the request needs. No test runs, lints, builds, or reviews unless explicitly asked.
- Retry a failed call at most once. If it still fails, state the failure in one sentence and stop — the presenter has a backup plan.
- Never ask clarifying questions mid-demo. Pick the most obvious interpretation and go.

## Output

- Reply in 2–4 short sentences: what was done, what was found. Lead with the result.
- No headers, no tables, no bullet lists unless the result is itself a list.
- Show key evidence inline (the one number, the one line of output) rather than describing it at length.

## Acceptable sacrifices

Thoroughness, defensive verification, edge-case sweeps, and documentation updates are all fair to skip. Correctness of the direct result is not — if speed and a correct answer genuinely conflict, correctness wins, briefly.
