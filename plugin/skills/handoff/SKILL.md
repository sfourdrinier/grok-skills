---
name: "handoff"
description: "Read a verified implementation handoff for a completed Grok code run (runId only; no apply)"
argument-hint: "--run-id <id>"
allowed-tools: "Bash(node:*)"
---

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool.
2. Set `SKILL_BASE` to that path.
3. Invoke only through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
node "$SKILL_BASE/run.mjs" handoff --run-id '<run-id>'
```

Return companion **stdout verbatim**.

<!-- plugin/skills/handoff.md -->

## What this is

`/grok:handoff --run-id <id>` is the **integration API** for multi-agent loops
(Claude Code + Codex as parents, Grok as peer implementer). It returns a
read-only envelope describing whether the code run produced an
**integration-ready** immutable patch + manifest.

| Command | Transfers | Key |
|---------|-----------|-----|
| `/grok:transfer` | Conversation context | session |
| `/grok:result` | Companion job output | job id |
| `/grok:handoff` | Implementation output | **`runId` only** |

## Notify is not ready

Completion **notifications** (1.5.0 toast/webhook) only mean a terminal attempt
finished. They are **not** permission to integrate. Always call `/grok:handoff
--run-id` and require dual-condition ready before applying anything.

## Dual-condition ready (required)

Observed `integration.ready` is true only when **all** hold:

1. Valid `implementation-handoff.json` with `integration.ready === true`
2. Patch file re-hashes to the manifest sha256
3. A **success** terminal envelope exists for the same `runId`

Missing envelope → `terminal-envelope-incomplete`. Tampered patch →
`artifact-integrity-failure`. No artifacts → `handoff-unavailable`.

## Parent integrate protocol (document only - never auto-apply)

This plugin **never** auto-applies, commits, merges, cherry-picks, or pushes.

1. Dispatch `/grok:code` with optional `--contract-file` (writeScopes + validation)
2. Wait for terminal status (`/grok:status --run-id` optional)
3. Run `/grok:handoff --run-id <id>`
4. Proceed only if envelope status is success and `response.integration.ready`
5. Verify `patch.sha256` matches on-disk patch
6. Inspect patch and changed files
7. Confirm parent base still contains `baseRevision` ancestry as needed
8. Check dirty overlap on paths you will touch
9. `git apply --check --binary <patch>`
10. Explicit `git apply --binary <patch>` (or equivalent) only with operator intent
11. Re-run relevant validation on the parent checkout
12. Record `runId` + patch hash in your notes

## What this mode never does

- Spawns Grok
- Creates companion jobs
- Sends notifications
- Writes the run directory
- Applies or commits changes

Raw arguments: `$ARGUMENTS`
