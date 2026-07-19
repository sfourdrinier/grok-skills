#!/usr/bin/env bash
# tools/verify.sh - one-command verification gate (dev tooling, not shipped in plugin/).
# CI runs the same suites (matrix) and tools/checks.sh; live modes and
# `claude plugin validate` remain local-only gates.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== wrapper suite =="
(cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q)

echo "== plugin suite =="
(cd plugin/scripts && node --test tests/*.test.mjs)

tools/checks.sh

echo "== plugin validate =="
if command -v claude >/dev/null 2>&1; then
  claude plugin validate ./plugin --strict
else
  echo "skip: claude CLI not on PATH (run locally before merge)"
fi

echo "VERIFY OK"
