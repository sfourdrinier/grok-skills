# PR3 internal code review (Gate D)

**Date:** 2026-07-16  
**Branch:** `feat/pr3-notifications-1.5.0`  
**Scope:** notifications 1.5.0  

## Spec compliance

| Requirement | Status |
|-------------|--------|
| Defaults `notificationMode: off`, webhook null | Pass - `DEFAULT_JOBS_CONFIG` |
| At-most-once attempt; no auto-retry pending | Pass - exclusive `wx` create; second call `already-attempted` |
| Always complete marker on send path | Pass - `completeMarker` after native/webhook |
| Never fail the job on notify errors | Pass - try/catch returns; companion swallows |
| auto only background | Pass - `shouldNotify` |
| Never status/setup/jobs alone | Pass - `NOTIFY_ELIGIBLE_MODES` |
| Context never to wrapper | Pass - `wrapperChildEnv` on spawn paths |
| skill-run.mjs unchanged | Pass - no edits |
| Operator retry not in PR3 | Pass - force reserved, not wired to auto path |
| Not exactly-once | Pass - docs/CHANGELOG |

## DRY

| Rule | Status |
|------|--------|
| Single marker writer | `notify.mjs` only |
| Single shouldNotify / adapters | same module |
| Prefs single default | `jobs.mjs` DEFAULT_JOBS_CONFIG |
| Companion thin hook | `maybeNotifyAfterTerminal` only |
| Skill env pattern one doc | `plugin/references/execution-context.md` |

## Quality notes

- Direct mode skips push notify (no durable `runs/<id>` for marker) - accepted residual; job still tracks result.
- Native notify on CI may fail (no display); marker still completed+failed - correct.
- Webhook tested with local HTTP server.

## Findings

None open remediable after self-review.

## Suites

- `node --test tests/notify.test.mjs tests/jobs.test.mjs` - pass  
- Full plugin scripts suite - run at package time  
