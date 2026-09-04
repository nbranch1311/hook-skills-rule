---
name: agent-factory
description: >-
  Interview the user to create or update an agent instruction file
  (AGENTS.md, CLAUDE.md, GEMINI.md, or a named/called agent). Use when
  the user wants a new agent file, to reshape an existing one from
  preferences, or says agent-factory.
disable-model-invocation: true
---

Interview me relentlessly about every aspect of this agent file until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time, waiting for feedback on each question before continuing. Asking multiple questions at once is bewildering.

If a _fact_ can be found by exploring the codebase, look it up rather than asking me. The _decisions_, though, are mine — put each one to me and wait for my answer.

Do not write or edit the agent file until I confirm we have reached a shared understanding.

## Before the interview

1. Ask: is this for a **project** agent file, a **global** agent file, or a **named/called** agent (invoked like a PR or code-review agent)?
2. Named/called only: ask for a name, then one sentence for when it should be used. If a named-agent directory already exists, offer existing files as update targets. If none, ask where this host keeps them. Project and global files do not need a name.
3. Detect existing instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`, and any named-agent directory). Project default if none: `AGENTS.md`. Global and named: ask if the path is unknown. Do not assume a Cursor-only layout.

## Interview

Start from what I already said. Then walk this skippable list; skip any dimension already answered or that does not fit the intent:

- communication
- autonomy (ask vs act)
- edit scope
- verification
- git/PR
- hard refusals

Don't add more dimensions unless the session surfaces a real extra one.

Split always-on agent instructions from scoped constraints. If a scoped-instruction directory already exists, recommend writing overflow there after I agree. If it does not, keep a one-line "when in `path/`" bullet in the agent file. Never create a product-specific rules tree just to park overflow.

Stern budget: keep the agent file under 200 lines. Going over risks agent bloat. Over 200 only if I insist, and warn first.

## After we agree

- **Create** if no file exists. **Patch** an existing file; do not silent-rewrite. Preserve unique rules I did not drop. From-scratch rewrite only if I say so.
- Sibling instruction files (e.g. `AGENTS.md` and `CLAUDE.md`): if identical or already syncing, edit one and confirm the other follows. If they differ, ask which to change. Don't silently give two hosts two different agents.
- No output template. On update, keep the file's structure and voice. On create, short markdown and tight bullets; add a heading only if it helps scan.
