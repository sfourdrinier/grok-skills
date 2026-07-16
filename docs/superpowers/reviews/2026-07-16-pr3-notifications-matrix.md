# PR3 failure-mode matrix + DRY boundaries (Gate A)

**Date:** 2026-07-16  
**PR:** feat/pr3-notifications-1.5.0  
**Authority:** design §11; plan rev 11 Gates A–E

## DRY modules (single owner)

| Behavior | Module / function |
|----------|-------------------|
| Jobs prefs defaults | `jobs.mjs` — `DEFAULT_CONFIG`, `getNotificationConfig` / `setNotificationConfig` |
| Execution context parse | `notify.mjs` — `getExecutionContext` |
| Whether to attempt | `notify.mjs` — `shouldNotify` |
| Marker I/O + attempt | `notify.mjs` — `attemptNotify` (only writer of `notified.json`) |
| Native / webhook adapters | `notify.mjs` — private helpers |
| Companion decide + call | `grok-companion.mjs` — thin `maybeNotifyAfterTerminal` only |
| Skill/agent env prefix text | `plugin/references/execution-context.md` (one pattern) |
| skill-run.mjs | **No change** |

## Failure-mode matrix

| Surface | Happy path | Crash / silent wrong | Bypass | External knobs | Fail closed / reason | Test |
|---------|------------|----------------------|--------|----------------|----------------------|------|
| `notified.json` pending | Exclusive create before send | Crash after pending before send → no auto-retry | Double completion | — | second call `already-attempted` | `notify.test.mjs` |
| `notified.json` complete | completed + sent/failed | Crash after send before complete → pending stuck | — | — | next auto path skips | `notify.test.mjs` |
| `off` | Never notify | — | misconfig | — | no-op | yes |
| `auto` + foreground | No notify | Missing context → FG | TTY inference forbidden | — | no-op | yes |
| `auto` + background | Native once if available | No native binary | — | PATH | completed+failed or skip no channel | yes |
| `native` | FG and BG attempt | Adapter missing | — | platform | completed+failed | yes |
| `webhook` | POST once | timeout / non-2xx | empty URL | URL | completed+failed; job not failed | yes |
| status/result/jobs/setup/preflight/cleanup | Never | Accidental hook | — | — | not called | companion tests |
| Wrapper child env | No context env | Leak of GROK_COMPANION_* | — | — | scrubbed | companion tests |
| skill-run.mjs | Unchanged | Accidental edit | — | — | review + tests | gate |

## Non-goals (this PR)

- Operator retry (PR5)
- Automatic retry of pending/failed
- Exactly-once delivery
- Windows native notify
