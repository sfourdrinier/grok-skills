<!-- docs/reviews/2026-07-15-adversarial-fix-plan.md -->

# Adversarial fix plan (agent + Grok reviews, 2026-07-15)

## Stance

- Fix every **actionable** finding from critical through low.
- **Do not fake-close** architectural residuals (D-SECRETREAD enforcement on current pin,
  co-user same-UID malware, finite regex secret coverage). Document those honestly.
- Method: workstreams with tests first where contracts change; no `claude` CLI automation.
- DRY: one source of truth for denylists, path modes, mode allowlists, web defaults.

## Workstreams

### WS-P0-GATE (critical)

| ID | Fix |
|----|-----|
| G1 | Gate blocks on structured findings severity or verify verdict; never bare review success |
| G2 | Stop hook forces hardened (or refuse gate when direct) |
| G3 | Persist run-mode only from `setup`; other `--run-mode` is one-shot |

### WS-P0-SECRETS (critical/high)

| ID | Fix |
|----|-----|
| S1 | Redact progress events on write (patterns + injected denylist) |
| S2 | Export denylist path/file for relay OR Python-only redact-on-write (prefer write-time) |
| S3 | Fail closed / hard warn when auth.json unparseable but non-empty |
| S4 | Casefold (and strip trivial wrappers) for denylist match |
| S5 | Scrub citation URLs (userinfo + sensitive query keys) |
| S6 | Status path uses same redaction as envelope where feasible |

### WS-P1-DEFAULTS

| ID | Fix |
|----|-----|
| D1 | `reason` default web **false**; force no-web when `--input` present |
| D2 | Gate-scripts-modified → classified failure (not success+warning) |
| D3 | Schema: wrapper-safe types (no multi-type `line` if unsupported) |
| D4 | Verify: stop calling hermetic if shell+net remain; optional cage shell later |
| D5 | Install offline/frozen: align flags or docs to truth |

### WS-P1-PLUGIN

| ID | Fix |
|----|-----|
| N1 | Skills: never unquoted `$ARGUMENTS` |
| N2 | Jobs dir/files 0700/0600; validate job id pattern; path containment |
| N3 | Cancel: kill real child tree; only mark cancelled if signals ok |
| N4 | Transfer: allowlist transcript roots, size cap, 0600 pack |
| N5 | Session stamp: workspace-keyed + 0600 |
| N6 | Companion mode allowlist; schema missing fail closed for adversarial |

### WS-P2-SANDBOX-STATE

| ID | Fix |
|----|-----|
| W1 | `git -c core.hooksPath=<empty>` (and scrub GIT_CONFIG_*) on worktree ops |
| W2 | Escape scan: always resolve + bound-check ignored artifacts |
| W3 | Preflight cache: re-check auth on hit; O_NOFOLLOW/atomic write |
| W4 | Expand secret patterns modestly (sk_, common prefixes); keep residual documented |
| W5 | Secret-shaped keys: private_key / signing_key family |
| W6 | Short auth leaves under known keys into denylist |

### WS-P3-DOCS-TESTS

| ID | Fix |
|----|-----|
| T1 | Unit tests for every contract change |
| T2 | SECURITY / OPEN-SECURITY / skills honesty |
| T3 | CHANGELOG + roadmap residual list |

## Out of scope as "fixed" (document only)

- OS-level secret **read** denial (needs pin/profile change)
- Wrapper-owned seatbelt instead of child telemetry (D3 redesign)
- Perfect secret pattern coverage
- Defense against same-UID local malware

## Order

P0 gate → P0 secrets → P1 defaults → P1 plugin → P2 sandbox → P3 docs/tests → tag.

## Status (post-hardening)

Actionable P0–P2 and leftovers (transfer allowlist, workspace session stamps) shipped.
Progress redact-on-write supersedes needing Node-side denylist for the progress path.
Still documented residuals: D-SECRETREAD, D3 telemetry trust, finite patterns, same-UID.
