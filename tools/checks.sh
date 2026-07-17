#!/usr/bin/env bash
# tools/checks.sh - mechanical repo checks (900-line cap + ASCII hyphens).
# Single source: called by tools/verify.sh locally and by the CI `mechanical`
# job. FAIL CLOSED: missing tools or tool errors fail the gate, never pass it.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v rg >/dev/null 2>&1 || {
  echo "ripgrep (rg) is required for checks.sh; install it (fail closed)"
  exit 1
}

echo "== 900-line cap (code files; tools/cap-allowlist.txt grandfathers, ratchets down) =="
allowlist="tools/cap-allowlist.txt"
fail=0
# One counting method everywhere (wc -l < file); NUL-safe for odd filenames.
while IFS= read -r -d '' f; do
  count="$(wc -l < "$f")"
  if [ "$count" -gt 900 ]; then
    if ! grep -qxF "$f" "$allowlist" 2>/dev/null; then
      echo "OVER 900 LINES (not grandfathered): $count $f"
      fail=1
    fi
  fi
done < <(find plugin \( -name '*.py' -o -name '*.mjs' \) -type f -not -path '*__pycache__*' -print0)
# Ratchet: a grandfathered file that dropped to <= 900 must leave the allowlist.
while IFS= read -r file; do
  [ -z "$file" ] && continue
  case "$file" in \#*) continue ;; esac
  if [ ! -f "$file" ]; then
    echo "STALE ALLOWLIST ENTRY (file gone): $file"
    fail=1
  elif [ "$(wc -l < "$file")" -le 900 ]; then
    echo "STALE ALLOWLIST ENTRY (now <= 900, remove it): $file"
    fail=1
  fi
done < "$allowlist"
[ "$fail" -eq 0 ] || exit 1

echo "== ASCII hyphens only (prose/comments; dated evidence archives exempt) =="
# docs/superpowers, docs/reviews, docs/research, docs/checklists are dated
# records - rewriting them would falsify evidence, so they are excluded.
# rg exits: 0 = match (violation), 1 = no match (pass), >1 = error (fail closed).
set +e
rg -n '[\x{2014}\x{2013}]' README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md AGENTS.md docs plugin \
  --glob '!*.svg' --glob '!*.png' \
  --glob '!docs/superpowers/**' --glob '!docs/reviews/**' \
  --glob '!docs/research/**' --glob '!docs/checklists/**'
rg_status=$?
set -e
if [ "$rg_status" -eq 0 ]; then
  echo "non-ASCII dash found (use ASCII hyphens; AGENTS.md rule 12)"
  exit 1
elif [ "$rg_status" -ne 1 ]; then
  echo "rg failed with exit $rg_status (fail closed)"
  exit 1
fi

echo "CHECKS OK"
