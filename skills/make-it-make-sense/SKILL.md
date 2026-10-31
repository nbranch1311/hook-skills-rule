---
name: make-it-make-sense
description: Walk finished work with the user until they understand the choices, agree with them, and can see what is missing.
argument-hint: "What finished work should we walk?"
disable-model-invocation: true
---

# Make it make sense

Walk work that is already done. The user learns the choices, agrees with them, and sees what is missing. Build the map, show it in one guide, then take one step per turn in chat.

## Terms

- **Map** — the sections, steps, and forks from the start of the work to the finish.
- **Fork** — a decision that could have changed the result. Rejected options stay on the map.
- **Anchor** — the file and line, heading, paragraph, or workflow step for this step. Read it before you cite it, and quote a few words from it. Quote the artifact or the conversation. When the source is recollection, say so.
- **Guide** — the surface that shows the map, highlights the walk cursor, and describes the previewed step.
- **Walk cursor** — the step the chat is on.
- **Preview** — the step showing in the guide. A click sets the preview. The walk cursor stays.
- **Return point** — a fork that still has an option which is neither visited nor waived. Say it in chat before every descent. It drops when every option on that fork is visited or waived.
- **Breadth** — describe each option at this fork, what it is for and how it works, and stop before its children.
- **Depth** — enter the chosen option and follow it to its result, then come back to the open options.
- **Question** — stay on this step and explain it again from the anchor.
- **Take Note** — a hunch to keep. Record the anchor, what feels off, and what is still unspecified.
- **Take Action** — a concrete change to run later. Record the anchor, the change, and what done means. When any of those is missing, record a Take Note instead.
- **Agree** — the user accepts this section, or the finished walk. Visiting a step leaves it visited. Agreement is this word.
- **Go to** — move the walk cursor to a step the user names. The guide phrase is `go to <id> <title>`. Steps jumped over stay ahead.
- **Waive** — mark this step waived. It stays on the map.
- **Gaps** — what a careful reading would expect here that the work does not have.

## 1. Bound the work

Name one artifact: a diff, a document, a diagram, or this conversation. Ask once when the artifact is ambiguous.

Done when the artifact has a name.

## 2. Build the map

Read the artifact. Lay the map out in walk order. Give every step and every fork option an anchor you have just read.

Done when every section, step, and fork is on the map with an anchor.

## 3. Open one guide

The session record is `.walk/<slug>/guide.html`, copied from this skill's [`guide.html`](guide.html). The slug is a short kebab-case name of the artifact. Replace the sample map in `#walk-session` before the user sees the file. The sample title is `Sample — replace this map`. Re-read the file from disk before each write, and replace only the JSON island.

One guide per session, and it is the only surface the user looks at.

Use a graph or map tool the user is already using when it can show the route, the current step, a short description, and the other steps. Update that tool from the session record, and leave the HTML file unopened.

Otherwise open `.walk/<slug>/guide.html`.

Match the JSON in the template:

- `cursor` is the walk cursor id. `traversal` is null until chosen, then `breadth` or `depth`. `later` holds ids the user wants to enter on a later descent. The session's `agreed` records the whole walk.
- A section has `agreed`. A step has `coverage` of `ahead`, `visited`, or `waived`, plus `note` and `action` text. Empty text means unused.
- `fork` is null or an array of options. An option is a step with `precondition` and `taken`. Nested `steps` are the path inside an option.

The guide computes return points from coverage. Leave an option `ahead` until it is visited or waived.

Done when the sample map is gone, the record matches the real map, the user can see that one guide, and chat is waiting.

## 4. Walk

Enter a section with its summary, its step count, and its fork count. Then take its first step. One step per turn. Wait.

Each turn names the section, the step id, the anchor, and the branch. Read the anchor again before you quote it. Chat carries how the step works and why it was chosen. The guide keeps the short description.

At each fork, list each option in one line: the option, its precondition, and whether it was taken. The first time, ask for breadth or depth and store it in `traversal`. Ask again when a fork has more than three options, or when the user asks.

On breadth, take the next ahead option at this fork: what it is for and how it works. Stop before its children. When the user wants a later descent into one option, append its id to `later`. On depth, enter the chosen option and follow it to its result, then come back to the options that are still ahead. Name the return point before a descent in either traversal.

Apply the move the user makes:

- **Question** — stay, and explain again from the anchor.
- **Take Note** — fill `note` with the anchor, what feels off, and what is still unspecified.
- **Take Action** — fill `action` with the anchor, the change, and what done means. The artifact stays unchanged until the queue runs.
- **Agree** — set the section's `agreed` when its steps are all visited or waived.
- **Go to** — set `cursor` to that id. Reload shows the new walk cursor. A click only changes the preview.
- **Waive** — set that step's `coverage` to `waived`.

Write `visited` when a step has been walked. Update the JSON island before you end the turn.

Done when the record matches the cursor, coverage, notes, actions, `later`, and `traversal`, and chat is waiting.

## 5. Close a section

Do this before the next section starts. Name the gaps. Then ask for Agree.

Done when `agreed` is set, or the user has left a note or an action and has told you to move on with the section still unagreed.

## 6. Finish

The walk is finished when every step is visited or waived, every section is agreed or explicitly left unagreed, and the gaps have been named. Ask Agree for the walk as a whole and set the session's `agreed`.

Show the Take Action queue. The user reorders or drops items. Re-read each anchor. Run the queue after the walk is finished and the user has confirmed the queue.

When the user wants the queue before the walk is finished, name the sections still ahead and say the queue may depend on them. Run the confirmed queue when they still want to stop. Leave ahead steps ahead.

Done when the confirmed queue has been carried out, or the user has dropped it, and the guide shows the coverage that remains.
