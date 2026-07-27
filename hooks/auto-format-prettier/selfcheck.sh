#!/usr/bin/env bash
# Runnable check for format.sh — fails if the stdin seam breaks.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
FMT="$DIR/format.sh"

echo '{"file_path":""}' | "$FMT"
echo '{}' | "$FMT"

tmp=$(mktemp "${TMPDIR:-/tmp}/auto-format-prettier.XXXXXX.js")
printf 'const x=1' >"$tmp"
echo "{\"file_path\":\"$tmp\"}" | "$FMT"

if [[ -x ./node_modules/.bin/prettier ]] || command -v npx >/dev/null 2>&1; then
  grep -q 'const x = 1' "$tmp" || {
    echo "selfcheck: expected prettier to rewrite file" >&2
    rm -f "$tmp"
    exit 1
  }
fi
rm -f "$tmp"

echo "selfcheck ok"
