#!/usr/bin/env bash
# afterFileEdit hook: prettier --write --ignore-unknown on the edited file.
# Always exit 0 — formatting is best-effort and must never block the agent.
set -u

if ! command -v jq >/dev/null 2>&1; then
  echo 'auto-format-prettier: jq not found — brew install jq' >&2
  exit 0
fi

file_path=$(jq -r '.file_path // empty')
[[ -n "$file_path" ]] || exit 0

run_prettier() {
  if [[ -x ./node_modules/.bin/prettier ]]; then
    ./node_modules/.bin/prettier --write --ignore-unknown "$1"
  elif command -v npx >/dev/null 2>&1; then
    npx prettier --write --ignore-unknown "$1"
  else
    return 127
  fi
}

out=$(run_prettier "$file_path" 2>&1) && exit 0
status=$?
if [[ $status -eq 127 ]]; then
  echo 'auto-format-prettier: prettier not found — install Node and prettier (or npx)' >&2
else
  # ponytail: one line is enough for Hooks output; full log if debugging
  printf '%s\n' "$out" | tail -n 1 >&2
fi
exit 0
