# PR3 adversarial + regression gate

**Date:** 2026-07-16  
**Branch:** `feat/pr3-notifications-1.5.0`  
**Purpose:** Close gaps that would re-trigger Codex; keep post-open hardening covered by regressions.

## Hardening (pre-open + post-adversarial)

| Risk | Fix |
|------|-----|
| Half-implemented `force` (PR5) invite findings | Removed force path from `attemptNotify` |
| Creating `runs/<id>` for notify | Refuse if run dir missing (`run-dir-missing`) |
| Wrapper env leak on preflight | `wrapperChildEnv` on spawn/spawnSync/preflight |
| Non-ASCII body / docs dashes | ASCII bodies; review docs ASCII-hyphenated |
| Path-unsafe run ids for notify/job joins | `safeRunIdForRunsDir` + companion `sanitizeRunId` |
| Terminal lifecycle `"running"` after exit | Exit-code / success/failure only |
| adversarial-review remapped to review for wrapper | `notifyMode` keeps skill name for payload |
| Invalid `--notification-mode` | Setup warn/no-op; prefs unchanged |
| Webhook URL leak on setup report | Host+path only (no userinfo/query) |
| plan vs prepare base drift | `prepare(..., base_revision=planned.base_revision)` |
| Missing regression coverage | Expanded notify + companion + isolation tests |

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
| NOTIFICATION_MODES stable | `notification mode set is complete and stable` |
| invalid mode normalizes to off | `setNotificationConfig rejects invalid mode` |
| adversarial-review skill mode in body | `adversarial-review is notify-eligible and webhook body uses skill mode` |
| no middle-dot in payload | `notify body is ASCII separators only` |
| skill env contract | `skills declare execution context prefix` |

### Companion + relay (`grok-companion.test.mjs`, `progress-relay.test.mjs`)

| Behavior | Test |
|----------|------|
| eligibility set | `notify eligibility excludes status/setup/...` |
| auto FG/BG policy | `auto notify is background-only` |
| run id shape shared | `run id shape used for notify is the same as progress-relay` |
| safe run id under runsDir | `safeRunIdForRunsDir accepts valid ids...` |
| setup invalid mode no-op | `setup rejects invalid --notification-mode without changing prefs` |
| setup webhook redaction | `setup redacts webhook URL query/userinfo from report` |
| no lifecycle running | `companion never maps terminal lifecycle to running` |

### Isolation (`tests/test_review_isolation.py`)

| Behavior | Test |
|----------|------|
| marker before worktree add | `test_owner_marker_written_before_worktree_add` |
| add failure cleans marker | `test_worktree_add_failure_removes_prewritten_marker` |
| pinned base_sha not HEAD | `test_dirty_patch_uses_pinned_base_not_live_head` |
| plan pin drives prepare | `test_prepare_honors_plan_base_revision_pin` |
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

## Post-Codex-simulation fixes (same branch)

| Finding | Fix |
|---------|-----|
| invalid setNotificationConfig clobber | leave prior prefs unchanged |
| setup exit ignores invalid mode | exit 1 when mode invalid |
| triple mode list | `notification-modes.mjs` shared module |
| dual-lens missing context | skill + contract test |
| code/reason/verify FG fences | export execution context |
| debate double notify | `skipNotify` on debate-a |
| prepare docstring HEAD | pinned base wording |
| job mode vs skill mode | createJob uses skill mode |
| PR body suite counts | updated on PR |
| setup partial notify prefs (R2-1) | atomic apply or apply none; accurate hint |
| skipNotify behavioral (R2-2) | `shouldAttemptTerminalNotify` + tests |
| notify imports jobs (R2-3) | tiny `notification-modes.mjs` |
| webhook scheme (R2-4) | parseWebhookUrl http(s) only |
| envelope parse (R2-5) | last JSON object line |
| dual-lens double notify (R2-6) | `--no-notify` on first pass |
| setup preflight exit (R2-7) | failed preflight rows fail setup exit |

## Gate criteria

- [x] Gate A matrix  
- [x] Gate B DRY  
- [x] Regression tests above green  
- [x] Full Node suite green  
- [x] Full Python suite green  
- [x] Docs refreshed (COMPATIBILITY/RELEASE/SECURITY/manual-smoke/references)  
- [x] No open remediable findings from internal adversarial pass  
- [x] Codex-simulation findings closed (table above)  
