# Over-conservatism audit (review UX)

Date: 2026-07-15  
Scope: fail-closed checks that discard a **finished, useful** Grok answer when
the failure is environmental noise or purity theater rather than a real safety
break.

## Product rule

**Deliver the review when Grok finished.** Prefer `status: success` +
`warnings[]` over failure envelopes that bury findings.

Never fail a read-only review solely because:

- paths on disk changed while the run ran (dev servers, logs, other editors)
- Grok listed change-shaped JSON keys (`changedFiles`, etc.) without proof of write
- the pre-run FS baseline could not be captured (git lock, etc.)

## Changes shipped (this pass)

| Check | Before | After |
|-------|--------|--------|
| FS fingerprint drift during review | `unexpected-edits` failure | Informational warning |
| Grok JSON change keys on review | `unexpected-edits` failure | Informational warning |
| Baseline capture failure before review | `worktree-failure` / blocked run | Soft-skip + note; review continues |

Hard enforcement for review remains: private auth home, sandbox verification,
version pin, model family, tool allowlist (read-only tools).

## Intentionally still hard-fail (not "too conservative")

| Area | Why keep fail-closed |
|------|----------------------|
| `code` / `verify` worktree escape / original-checkout mutation | Real write-safety boundary |
| Sandbox verification failure | Write confinement not proven |
| Auth missing / version mismatch / probe-required | Cannot run honestly |
| Model family mismatch | Wrong model ≠ requested product |
| Auth home teardown failure after success | Credential residue risk |
| Malformed / missing Grok output | No deliverable body |
| Schema mismatch when caller requested schema | Contract with caller |
| Opt-in stop-review gate (critical/high) | Explicit user opt-in to block |
| Gate scripts modified (`code`) | Refuse running Grok-rewritten build scripts (result still returned) |

## Companion / plugins (not wrapper)

| Area | Notes |
|------|--------|
| Codex agents need SessionStart install | Host limitation (openai/codex#18988); auto-ensure on SessionStart |
| Skills inventing cache paths | Docs/skill: use `PLUGIN_ROOT` / absolute companion only |
| Stop gate free-text allow | Already fail-closed structured (intentional) |

## Follow-ups (optional, not blocking)

1. Demote post-run **model family** mismatch to warning+success when body is good? **No for now** — operators pin models for a reason.
2. Demote **sandbox verify** fail to warning after success? **No** — that is the write-confinement proof.
3. Stop fingerprinting gitignored trees for the drift *note* to reduce noise? Optional later; note is cheap and useful for the Next/.pid case.

## Regression tests

See `plugin/wrapper/scripts/tests/test_mode_review.py`:

- `test_review_filesystem_write_is_warn_not_failure`
- `test_review_file_change_in_output_is_informational`
- `test_review_fs_baseline_capture_failure_does_not_block_review`
