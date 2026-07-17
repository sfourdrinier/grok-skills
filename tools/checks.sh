#!/usr/bin/env bash
# tools/checks.sh - mechanical repo checks (900-line cap + ASCII hyphens).
# Single source: called by tools/verify.sh locally and by the CI `mechanical`
# job. Needs ripgrep on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 900-line cap (code files; tools/cap-allowlist.txt grandfathers, ratchets down) =="
allowlist="tools/cap-allowlist.txt"
fail=0
while IFS= read -r line; do
  count="$(echo "$line" | awk '{print $1}')"
  file="$(echo "$line" | awk '{print $2}')"
  [ "$file" = "total" ] && continue
  if [ "$count" -gt 900 ]; then
    if ! grep -qxF "$file" "$allowlist" 2>/dev/null; then
      echo "OVER 900 LINES (not grandfathered): $count $file"
      fail=1
    fi
  fi
done < <(find plugin -name '*.py' -o -name '*.mjs' | grep -v __pycache__ | xargs wc -l)
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
if rg -n $'[\x{2014}\x{2013}]' README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md AGENTS.md docs plugin \
  --glob '!*.svg' --glob '!*.png' \
  --glob '!docs/superpowers/**' --glob '!docs/reviews/**' \
  --glob '!docs/research/**' --glob '!docs/checklists/**' 2>/dev/null; then
  echo "non-ASCII dash found (use ASCII hyphens; AGENTS.md rule 12)"
  exit 1
fi

echo "CHECKS OK"
