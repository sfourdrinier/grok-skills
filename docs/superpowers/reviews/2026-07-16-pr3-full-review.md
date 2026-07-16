# PR3 full internal code review (Gate D) + PR2 late-fix carry

**Date:** 2026-07-16  
**Branch:** `feat/pr3-notifications-1.5.0`  
**Commits reviewed:** `44d7bf3` (notifications), `a930b63` (PR2 late isolation), docs/review update

## Scope

1. PR3 completion notifications (1.5.0)  
2. Late PR2 Codex isolation findings carried into this branch  
3. Documentation completeness (no stale 1.3-only story)

---

## A. Spec compliance - notifications

| Requirement | Verdict | Evidence |
|-------------|---------|----------|
| Defaults off / webhook null | Pass | `DEFAULT_JOBS_CONFIG` in `jobs.mjs` |
| At-most-once attempt | Pass | `createPendingMarker` flag `wx`; second call `already-attempted` |
| Crash-left pending not auto-retried | Pass | existing marker blocks; test |
| Never fail job on notify | Pass | try/catch in `attemptNotify` + companion swallow |
| auto only background | Pass | `shouldNotify` |
| native / webhook adapters | Pass | osascript / notify-send / http(s) POST; shell false |
| Never status/setup/jobs alone | Pass | `NOTIFY_ELIGIBLE_MODES` |
| Context never to wrapper | Pass | `wrapperChildEnv` on spawn/spawnSync |
| skill-run.mjs no change | Pass | diff empty vs main |
| Not exactly-once; no operator retry product | Pass | docs + no force from companion |
| Direct mode | Accepted residual | No durable `runs/<id>` → skip push; job still stored |

## B. Spec compliance - isolation follow-ups (late PR2)

| Finding | Verdict | Evidence |
|---------|---------|----------|
| Marker before worktree add | Pass | `write_owner_marker_file` then `_git worktree add`; test order |
| Dirty patch vs pinned base | Pass | diff argv uses `base_sha` not `HEAD`; test |
| Retain marker if worktree remains | Pass | early return in cleanup; test |
| ITA all-zero OID (SHA-256) | Pass | `_is_all_zero_oid`; test 40/64 |
| Status porcelain as bytes | Pass | `_run_git_bytes` + surrogateescape |
| Prior rounds (ext-diff, submodule ignore, ITA literal, pre-plan CAS, .diff reap) | Pass | already on main + branch |

## C. DRY

| Rule | Verdict |
|------|---------|
| Single `notified.json` writer | `notify.mjs` only |
| Single prefs defaults | `DEFAULT_JOBS_CONFIG` |
| Companion thin | `maybeNotifyAfterTerminal` only |
| One execution-context pattern | `plugin/references/execution-context.md` |
| Isolation helpers not duplicated for notify | N/A separate subsystem |

## D. Documentation audit (updated this pass)

| Doc | Was outdated? | Action |
|-----|---------------|--------|
| Packaging triple | OK 1.5.0 | kept |
| CHANGELOG | 1.5.0 + isolation Fixed | kept / already had Fixed |
| README | setup line only | setup + notifications path |
| docs/COMPATIBILITY.md | missing 1.4/1.5 | **added** isolation + notifications sections |
| docs/RELEASE.md | smoke 1.3 only | **added** 1.4 + 1.5 smoke |
| SECURITY.md | no webhook surface | **added** notifications limits |
| plugin/references/README.md | no notify | **added** section + setup note |
| plugin/references/manual-smoke.md | no notify/isolated | **added** checks |
| plugin/references/execution-context.md | present | kept |
| skills/agents/codex-agents | partial env | **strengthened** code BG + codex toml |
| design status line | PR2 still "opt-in" only | **noted** PR2 shipped / PR3 in flight |
| plan isolation dirty line | still HEAD | **updated** to pinned base_sha |
| authority-policies / wrapper SKILL | isolation already | OK |

## E. Issues found in review (and disposition)

| Severity | Issue | Disposition |
|----------|-------|-------------|
| Low | `force=true` with `notificationMode=off` still attempts native if PR5 mis-wires | Accepted residual until PR5; companion never passes force |
| Low | Direct mode no push notify | Documented accepted residual |
| Info | Headless native often fails | Marker still `completed`+`failed` - correct |

No open remediable P0/P1. Low items documented.

## F. Suites (re-run before final commit)

- Isolation unit tests: 22 OK (prior)  
- Node: `tests/*.test.mjs` including notify/jobs  
- Python: full discover (prior on branch)

## G. Done criteria (Gate E ready)

- [x] Matrix  
- [x] DRY  
- [x] Internal review artifact (this file)  
- [x] Docs aligned  
- [ ] Open GitHub PR / merge  
- [ ] Tag `v1.5.0` after main  
