# PR3 adversarial + regression gate (pre-open)

**Date:** 2026-07-16  
**Branch:** `feat/pr3-notifications-1.5.0`  
**Purpose:** Close gaps that would re-trigger Codex on open PR.

## Hardening applied before open

| Risk | Fix |
|------|-----|
| Half-implemented `force` (PR5) invite findings | Removed force path from `attemptNotify` |
| Creating `runs/<id>` for notify | Refuse if run dir missing (`run-dir-missing`) |
| Wrapper env leak on preflight | `wrapperChildEnv` on preflight spawn |
| Non-ASCII body / docs dashes | ASCII bodies; review docs ASCII-hyphenated |
| Missing regression coverage | Expanded notify + isolation tests (see below) |

## Regression matrix -> tests

### Notifications (`plugin/scripts/tests/notify.test.mjs`)

| Behavior | Test |
|----------|------|
| defaults off | jobs prefs + shouldNotify off |
| auto FG no-op | `auto mode skips in foreground` |
| auto BG eligible | shouldNotify matrix |
| already-attempted | first native/webhook then second skip |
| crash-left pending | pre-seed pending marker |
| webhook success | local HTTP 204 |
| webhook fail completes marker | HTTP 500 then already-attempted |
| ineligible mode | status never writes marker |
| missing run dir | no create |
| wrapperChildEnv pure | does not mutate input |

### Isolation (`tests/test_review_isolation.py`)

| Behavior | Test |
|----------|------|
| marker before worktree add | `test_owner_marker_written_before_worktree_add` |
| add failure cleans marker | `test_worktree_add_failure_removes_prewritten_marker` |
| pinned base_sha not HEAD | `test_dirty_patch_uses_pinned_base_not_live_head` |
| retain marker if wt remains | `test_cleanup_retains_marker_if_worktree_still_present` |
| SHA-256 zero OIDs ITA | `test_intent_to_add_detects_sha256_zero_oids` |
| status bytes non-UTF-8 | `test_intent_to_add_status_uses_bytes_not_utf8_decode` |
| ITA literal pathspec | `test_ita_pathspec_metachar_does_not_exclude_tracked_dirty` |
| no-ext-diff | `test_isolation_diff_disables_external_diff` |
| ignore-submodules=none | `test_dirty_submodule_rejected` |
| plan CAS before prepare | `test_isolation_identity_recorded_before_prepare` |
| sibling .diff reaped | `test_remove_external_worktree_deletes_sibling_diff` |

## Residual accepted (document, do not open Codex loop)

1. Direct mode: no durable `runs/<id>` -> no push notify (job still tracked).  
2. Headless native often fails; marker still `completed`+`failed` (correct).  
3. Webhook may target private IPs if operator sets URL (operator-trusted config).  

## Open PR criteria

- [x] Gate A matrix  
- [x] Gate B DRY  
- [x] Regression tests above green  
- [x] Full Node suite green  
- [x] Full Python suite green  
- [x] Docs refreshed (COMPATIBILITY/RELEASE/SECURITY/manual-smoke/references)  
- [x] No open remediable findings from internal adversarial pass  
