# PR4 adversarial round-2 (post-implementation)

**Date:** 2026-07-16  
**Against:** working tree on `feat/pr4-implementation-handoff-1.6.0` after Gate D

## Attack surface re-check

| Attack | Result |
|--------|--------|
| Handoff spawns Grok | No — read-only mode; companion passthrough only |
| Job / notify on handoff | No — dedicated runHandoff, not captureAndTrack |
| Ready without envelope | dual_condition_ready requires success envelope |
| String-prefix scope escape | path_in_scopes component prefix; tests |
| Contract symlink | load rejects symlink |
| Truncated oversized patch | fail closed artifact-too-large |
| Second finalizer pipeline | none — code_handoff_finalize only |
| Companion bloat | 872 ≤ 900 |
| Notify mistaken for ready | docs + skills forbid |
| Auto-apply | not implemented |

## Residual non-blocking notes

1. Direct-mode + `--contract-file` is only meaningful on wrapper code path; companion direct without wrapper cannot produce handoff artifacts (acceptable: hardened path is required for ready handoff).
2. `no-changes` yields success code envelope with ready false — intentional for empty runs and existing tests.
3. Live dual-host UI smoke remains operator checklist (automated unit e2e covers dual-condition + scope + artifacts).

## Verdict

Ship-ready for PR open / merge then tag v1.6.0. Zero remediable open findings.
