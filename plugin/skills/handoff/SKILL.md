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

Observed `integration.ready` is true only when **all** hold (wrapper authority;
do not reimplement a weaker check):

1. Valid `implementation-handoff.json` with `integration.ready === true`
   (also requires non-empty `changedFiles`, empty blockers, validation flags
   true, and `patch.bytes > 0`)
2. A **success** terminal envelope for the same `runId` with **`mode: "code"`**
3. Envelope `baseRevision` is non-empty and **equals** the manifest base
4. Patch file exists under the run dir, size matches `patch.bytes` (> 0), and
   sha256 re-hashes to the manifest
5. Manifest `changedFiles` matches paths in the patch headers (and envelope
   `changedFiles` destinations when that list is present)

Missing/wrong-mode envelope → `terminal-envelope-incomplete`. Null base,
size/hash mismatch, or path-set mismatch → integrity failure. No artifacts →
`handoff-unavailable`.

## Hardened only

Durable handoff artifacts exist only after a **hardened** `code` run. Direct
run-mode does not write verified handoff state; use `setup --run-mode hardened`
(or the companion's hardened default) before expecting `/grok:handoff` ready.

## Parent integrate protocol (mode-aware; handoff is read-only)

This skill is **read-only** - it never applies. How results land is mode-aware
(canonical: `plugin/references/integration-modes.md`):

- **direct:** source edits already live; handoff artifacts may be absent on
  pure live-tree paths; review the working tree diff
- **auto:** companion may auto-apply after dual-condition ready + apply-time
  revalidation (you still may call handoff to observe ready)
- **review:** never auto-applies; parent apply is manual after ready

This plugin **never** auto-commits, merges, cherry-picks, or pushes in any mode.

### Manual parent apply (review / when auto did not apply)

This flow is **code-mode only**: `/grok:handoff` refuses peer runIds. A peer run
is finalized by `peer stop` itself (use its response as the ready signal); it
never routes through `/grok:handoff`.

Canonical parent-apply checklist (do not fork the algorithm):
[implementation-handoff.md](../../references/implementation-handoff.md)
Parent apply checklist. Operator summary:

1. `/grok:handoff --run-id` success + `response.integration.ready`
2. Confirm parent base still present / ancestry
3. Dirty overlap check on target paths (`git status --porcelain -z` inventory)
4. Explicit patch integrity recheck: on-disk patch bytes/size/sha still match the
   handoff manifest (same integrity gate auto/peer re-run before apply)
5. `git apply --check --binary <patch>`
6. Explicit `git apply --binary <patch>` only with operator intent
7. Re-run project validation on the parent checkout
8. Record `runId` + patch sha256

## What this mode never does

- Spawns Grok
- Creates companion jobs
- Sends notifications
- Writes the run directory
- Applies or commits changes

Raw arguments: `$ARGUMENTS`
