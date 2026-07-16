# Run lifecycle, isolated review, completion signals, and implementation handoff

**Status:** design revision 5 (PR1–PR4 fully locked — no open decisions; PR4 expanded to executable detail)  
**Date:** 2026-07-15  
**Product:** grok-skills (Claude Code + Codex)  
**Baseline:** v1.2.10  

## 1. Problem

Background and long-running Grok modes (especially `review`) are hard to trust:

1. Lifecycle truth is fuzzy (success vs unfinished vs failed).
2. Run id is advertised before a durable seed `run.json` exists.
3. Post-Grok finalization is quiet; operators think the process is stuck.
4. Finalization can hang with no terminal envelope.
5. Review evidence can be poisoned by concurrent writers on the live checkout.
6. No clean completion signal for background jobs.
7. `code` retains a dirty worktree but does not produce a verified, immutable
   implementation artifact a parent harness (Codex/Claude) can safely inspect,
   hash, and integrate. A retained worktree can change after completion, loses
   provenance, and drops untracked/binary files easily.

## 2. Goals

- Durable target lifecycle for every live run.
- Atomic seed `run.json` before the run id is published.
- Atomic, validated terminal `envelope.json` before a run is finished.
- Status projection per §6 (both, versioned).
- Phase progress with `elapsedMs`.
- Process-based finalization watchdog with classified terminal failure.
- Isolated review for `--base` and `--isolated`; fail closed on isolation failure.
- Optional notifications after terminal envelope; at-most-once; never fail the run.
- Dual-host: same core; harnesses only present.
- Docs follow code on every shippable PR (AGENTS.md rule #1).
- Verified immutable `code` handoff: contract scopes, unexpected-commit check,
  git-binary patch, `integration.ready`, read-only `/grok:handoff --run-id`.

## 3. Non-goals

- Chat injection into Claude or Codex from the wrapper or companion.
- Broad ignore lists as the read-only safety model.
- A second durable stream besides `progress.jsonl`.
- Failing a completed run because notification delivery failed.
- Applying untracked files under `--isolated` (v1).
- Auto-apply, auto-commit, merge, cherry-pick, or push of handoff patches.
- `--allow-commits` for code mode (future; out of PR4).
- Repurposing `/grok:transfer` for implementation output.

## 4. Locked decisions

| Topic | Decision |
|-------|----------|
| Lifecycle representation | `run.json.lifecycle` and `response.target.lifecycle` are source of truth. Top-level envelope `status` is a projection (§6 table only). |
| Seed record | `lifecycle: "created"`, `status: "running"` (never `status: "created"`). |
| Terminal persist | `persist_terminal_envelope(paths, envelope, *, lifecycle)` where `lifecycle` is exactly one of `completed`, `failed`, `canceled`. Caller always passes it. |
| Interrupted | Status mode **always** best-effort atomic-writes `lifecycle: "interrupted"` when owner is dead and no valid envelope. If that write fails, response still reports `lifecycle: "interrupted"` and top-level `failure`. |
| Finalization | Child process via `multiprocessing.get_context("spawn")`; parent `join(timeout)`; kill on timeout. Worker **writes** the terminal envelope. Parent does not re-promote. Progress during finalize: **parent-only** progress events around join (enter/timeout/success); worker does not append progress. |
| Review isolation | See §10. No silent live-checkout fallback. |
| Notifications storage | **Only** `plugin/scripts/lib/jobs.mjs` index `config` (with `runMode`). Never gate-state. |
| Notifications default | `off`. Setup flags set mode. Setup copy recommends `auto`. |
| Notify at-most-once | Exclusive `notified.json` with states `pending` then `sent` (§11). |
| Native notify | argv-only spawn, `shell: false`, 5s timeout, platforms §11. |
| PR versions | PR1 **1.3.0**, PR2 **1.4.0**, PR3 **1.5.0**, PR4 **1.6.0**. |

## 5. Architecture

```text
Companion → Wrapper → state_root/runs/<id>/
  run.json, progress.jsonl, envelope.json, owner.json, owner.pid, notified.json
```

Worktrees for isolation: `state_root/worktrees/review/<runId>/` (absolute under `runstate.state_root()`).

## 6. Lifecycle and status projection

### Lifecycle values on `run.json.lifecycle`

| Value | Meaning |
|-------|---------|
| `created` | Seed written; run id may be published |
| `running` | Active work before post-model finalize |
| `finalizing` | After Grok exit; packaging terminal result |
| `completed` | Valid success envelope persisted |
| `failed` | Valid failure envelope persisted |
| `canceled` | Operator cancel with terminal envelope |
| `interrupted` | Written by status when process dead and no valid envelope |

### Transition graph (allowed writes)

```text
created → running | failed | canceled
running → finalizing | failed | canceled
finalizing → completed | failed | canceled
(any non-terminal) → interrupted   # status-mode only, when process dead + no envelope
```

Terminal lifecycles `completed`, `failed`, `canceled`, `interrupted` are **immutable** once set (further `set_lifecycle` raises or no-ops with log; tests require refuse overwrite).

### Top-level status projection

| Target lifecycle | Top-level `status` | Status-mode exit |
|------------------|--------------------|------------------|
| `created`, `running`, `finalizing` | `running` | 0 |
| `completed` | `success` | 0 |
| `failed`, `canceled`, `interrupted` | `failure` | 1 |
| Cannot load/own run; stored envelope unreadable or invalid C4 | `failure` | 1 |

Status mode always emits `mode: "status"` with a well-formed envelope. Live modes still emit their own terminal envelopes with `success`/`failure` for the *run* result.

### Seed `run.json` (exact fields)

```json
{
  "schemaVersion": 1,
  "runId": "<id>",
  "mode": "<mode>",
  "createdAtUtc": "<iso-z>",
  "lifecycle": "created",
  "status": "running",
  "requestedModel": null,
  "repository": null,
  "targetWorkspace": null,
  "worktreePath": null,
  "worktreeBranch": null,
  "baseRevision": null,
  "progressStreamPath": "<absolute progress.jsonl>",
  "envelopePath": "<absolute envelope.json>"
}
```

## 7. Durable invariants

1. `[grok-run-id]` is emitted only after atomic seed `run.json` exists.  
2. Finished only after atomic validated `envelope.json`.  
3. `persist_terminal_envelope` always receives explicit terminal lifecycle.  
4. JSON writes: temp sibling `path.name + ".tmp." + pid` then `os.replace`, file mode 0600.  
5. Progress: only `progress.jsonl`.

## 8. Progress

### Phases (exact order vocabulary)

`start` | `validate` | `authhome` | `prepare` | `grok` | `finalizing` | `notify` | `done`

`notify` is companion-side only (optional); wrapper may omit it.

### Event fields

Every event: `schemaVersion`, `runId`, `seq`, `ts`, `phase`, `level`, `message`, plus `elapsedMs` (int, from `createdAtUtc`).

### Status `response.target` (exact keys)

```json
{
  "lifecycle": "finalizing",
  "process": "alive",
  "elapsedMs": 181492,
  "lastProgressAt": "2026-07-16T02:15:11+00:00",
  "lastEvent": { "seq": 12, "phase": "finalizing", "message": "entering finalization", "ts": "2026-07-16T02:15:11+00:00", "elapsedMs": 181492 },
  "recentEvents": [],
  "eventCount": 42,
  "resultAvailable": false,
  "hasStoredEnvelope": false,
  "recordStatus": "running",
  "mode": "review",
  "requestedModel": "grok-4.5",
  "repository": "/path",
  "runDir": "/path/to/run"
}
```

`recentEvents`: last **8** events as compact summaries (same shape as `lastEvent`).  
`resultAvailable`: true iff valid stored envelope exists.

## 9. Finalization watchdog

### Mechanism (exact)

1. After Grok child exits, parent: `set_lifecycle(paths, "finalizing")`; progress `finalizing` / "entering finalization".  
2. Parent writes worker payload JSON (0600) to `paths.run_dir / "finalize-payload.json"` containing run_id, paths, mode context needed to build the envelope.  
3. Parent starts:

```python
ctx = multiprocessing.get_context("spawn")
proc = ctx.Process(target=finalize_worker_main, args=(str(payload_path),), name="grok-finalize")
proc.start()
proc.join(timeout=budget_seconds)
```

4. If `proc.is_alive()`: `proc.terminate(); proc.join(5)`; if still alive `proc.kill(); proc.join(5)`. Parent builds failure envelope class `finalization-timeout`, calls `persist_terminal_envelope(..., lifecycle="failed")`, progress done, return failure.  
5. If process exited 0 and `envelope.json` validates: parent returns that envelope (already persisted by worker).  
6. If process exited non-zero without valid envelope: parent writes `finalization-timeout` or `cli-failure` with detail from worker stderr file `paths.run_dir / "finalize-worker.stderr"`; lifecycle `failed`.

### Worker responsibilities

- Load payload, perform sandbox verify, drift, build success/failure envelope for the mode, `persist_terminal_envelope` with correct lifecycle (`completed` or `failed`).  
- Does **not** write progress.jsonl (parent owns progress around the join).  
- Exit 0 only if envelope persisted and valid.

### Budgets

| Modes | Seconds |
|-------|---------|
| review, reason, adversarial-review (maps to review) | 120 |
| code, verify | 180 |
| preflight, status, cleanup | no finalize worker |

Env `GROK_FINALIZE_TIMEOUT_SECONDS`: integer, clamp **30..600**, overrides table when set.

## 10. Isolated review

### When

| Flags | Action |
|-------|--------|
| `--base` set | Isolation required |
| `--isolated` set | Isolation required |
| both | Isolation required (same as base path for worktree; still apply dirty if working tree differs from HEAD — **locked:** when both set, worktree at HEAD, apply tracked dirty, keep `--base` for comparison) |
| neither | Live checkout; drift warnings only |

### Worktree path

`{state_root}/worktrees/review/{run_id}` where `state_root = runstate.state_root()`.

### `--base` (exact)

1. `git -C repo_root worktree add --detach {worktree_path} HEAD`  
2. On non-zero: raise `isolation-unavailable`.  
3. Review `cwd` / target workspace = worktree_path.  
4. Pass original `--base` through unchanged to comparison/prompt logic.  
5. `finally`: `git -C repo_root worktree remove --force {worktree_path}` then `rmtree` if needed; failures log only.

### `--isolated` without `--base` (exact)

1. Same worktree add at HEAD.  
2. Diff file: `{worktree_path}.diff` under state temp next to worktree, mode 0600:  
   `git -C repo_root diff HEAD --binary` → write file.  
3. If file size > 0:  
   `git -C worktree_path apply --whitespace=nowarn {diff_path}`  
   Non-zero exit → `isolation-unavailable` (cleanup worktree first).  
4. Empty diff: continue.  
5. Untracked files: **never** copied.  
6. Cleanup worktree + delete diff file in `finally`.

### Return type when isolation not required

`prepare_review_isolation` returns `None` when neither flag is set. Type is `Optional[ReviewIsolation]`.

## 11. Notifications

### Storage (exact)

File: jobs index used by `jobs.mjs` (`index.json` under jobs dir). Config object always includes:

```json
{
  "runMode": "hardened",
  "notificationMode": "off",
  "notificationWebhookUrl": null
}
```

Defaults when missing keys: `notificationMode: "off"`, `notificationWebhookUrl: null`.

### Modes (exact)

| Mode | Behavior |
|------|----------|
| `off` | No notify |
| `auto` | Notify only if job was **background** and a native channel is available |
| `native` | Always attempt native (foreground or background) when channel available |
| `webhook` | POST to `notificationWebhookUrl` if non-null non-empty URL; else no-op log |

### At-most-once (exact)

File: `{runDir}/notified.json`.

1. Try create exclusive with content `{"state":"pending","at":"<iso>"}`.  
2. If file exists:  
   - `state=="sent"` → return `{attempted:false, sent:false, reason:"already-sent"}`  
   - `state=="pending"` and age &lt; 300s → return skip `pending-inflight`  
   - `state=="pending"` and age ≥ 300s → proceed to send (retry)  
3. Send.  
4. Overwrite marker `{"state":"sent","at":"<iso>"}` via atomic write.  
5. On send failure: leave `pending` (or rewrite pending with new `at`); return `{attempted:true, sent:false}`; never throw to fail the job.

### Native adapters (exact)

| Platform | Command argv | Timeout |
|----------|--------------|---------|
| Darwin | `["osascript", "-e", "display notification \"" + escape(body) + "\" with title \"" + escape(title) + "\""]` where escape only backslash-escapes `\` and `"` | 5000 ms |
| Linux | `["notify-send", "--", title, body]` if `notify-send` on PATH | 5000 ms |
| Windows | Skip native (no-op, reason `windows-native-unsupported` in v1) | n/a |
| Other | no-op | n/a |

Always `shell: false`. Title fixed string `Grok Skills`. Body: `"{mode} {lifecycle} · {runId} · {durationSeconds}s"`.

### Webhook (exact)

- Method POST, `Content-Type: application/json`  
- Body: `{"runId","mode","lifecycle","durationSeconds"}` only  
- Timeout 3000 ms  
- URL from config only  

### When companion notifies

- After **background** job wrapper process closes, and run has terminal envelope or terminal lifecycle, if preference is `auto` or `native` or `webhook`.  
- For `native` preference: also after **foreground** terminal (explicit user choice).  
- Never on `status` / `result` / `jobs` / `setup` alone.  
- Resolve `runDir` from `XDG_STATE_HOME`/state_root + runId from job record or envelope.

## 12. Error classes

| Class | Lifecycle when terminal | PR |
|-------|-------------------------|-----|
| `isolation-unavailable` | `failed` | PR2 |
| `finalization-timeout` | `failed` | PR1 |
| `implementation-contract-invalid` | `failed` | PR4 |
| `write-scope-violation` | `failed` | PR4 |
| `unexpected-commit` | `failed` | PR4 |
| `artifact-generation-failure` | `failed` | PR4 |
| `artifact-integrity-failure` | `failed` (handoff mode) | PR4 |
| `handoff-unavailable` | `failed` (handoff mode) | PR4 |

Add all of the above to `envelope.ERROR_CLASSES` when the owning PR lands.

## 13. Four PRs

| PR | Version | Scope |
|----|---------|--------|
| PR1 | 1.3.0 | Lifecycle, seed, persist, status projection, progress, process finalize |
| PR2 | 1.4.0 | Isolated review |
| PR3 | 1.5.0 | Notifications |
| PR4 | 1.6.0 | Verified implementation handoff for `code` |

## 14. PR4 — Verified implementation handoff

### 14.1 Purpose

Make Grok `code` usable as a peer implementation agent for Codex and Claude.

A successful code run must produce a **verified, immutable handoff artifact**.
The parent harness must be able to prove:

| Proof | Source |
|-------|--------|
| Exact committed revision Grok started from | `baseRevision` (full SHA) |
| Paths Grok was allowed to modify | contract `writeScopes` (when provided) |
| What files and Git modes changed | handoff `changedFiles` + patch |
| Whether Grok created commits unexpectedly | HEAD == `baseRevision` after exit |
| Which validation commands ran and outcomes | envelope `commands` + evidence tails |
| Whether wrapper-owned gates passed | build gate + requiredValidation + ready |
| Whether the artifact is intact | patch SHA-256 re-read + handoff verify |
| Whether result is safe to consider for integration | `integration.ready` + `blockers` |
| Which worktree and run own the result | `runId` + worktree path/branch |

The **parent** remains responsible for reviewing and integrating. PR4 must
**never** automatically commit, merge, cherry-pick, push, or modify the parent
checkout.

| Command | Transfers | Key |
|---------|-----------|-----|
| `/grok:transfer` | Claude session **conversation context** only | session (unchanged) |
| `/grok:result` | Companion job output | companion job ID (UI convenience) |
| `/grok:handoff` | **Implementation output** (immutable patch + schema) | wrapper **`runId` only** |

Do not repurpose or rename `/grok:transfer`. Companion job ID must not be
required for handoff when `runId` is known.

### 14.2 Existing implementation facts (grounding)

| Component | Owns |
|-----------|------|
| `plugin/skills/code/SKILL.md` | Code mode: isolated external worktree, uncommitted, retained |
| `plugin/wrapper/scripts/groklib/modes/code.py` | Sentinel validation, diff confinement, deps install, build gate |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | code/verify lifecycle and envelope assembly |
| `plugin/wrapper/scripts/groklib/worktree.py` | External worktree create, ownership markers, base validation, cleanup |
| Existing C4 envelope fields | `baseRevision`, `changedFiles`, `diffSummary`, `commands`, `worktreePath`, `worktreeBranch` |

PR4 builds on these. It does not re-implement worktree isolation from scratch.

### 14.3 Machine-readable implementation task contract

#### CLI

Optional but recommended: `--contract-file <path>` on `code` mode.

Keep existing `--task` / `--task-file`: the task file explains the work; the
contract defines **machine-enforced boundaries**.

#### Contract schema (exact)

```json
{
  "schemaVersion": 1,
  "taskId": "voice-policy-shared-contract",
  "objective": "Implement the shared ImagiExplain voice policy contract",
  "target": ".",
  "writeScopes": [
    {
      "path": "packages/sharedSchemas/src/imagibooks/imagiexplainAdminPreview.ts",
      "kind": "file"
    },
    {
      "path": "packages/sharedSchemas/src/imagibooks/imagiexplainAdminPreview.test.ts",
      "kind": "file"
    }
  ],
  "acceptanceCriteria": [
    "Only provider-default ElevenLabs Multilingual v2 and Gemini 2.5 Flash Preview TTS classify as public",
    "Designed and cloned voices do not classify as public"
  ],
  "requiredValidation": [
    {
      "argv": ["pnpm", "--filter", "@shared/schemas", "typecheck"],
      "cwd": ".",
      "purpose": "shared schemas typecheck"
    }
  ]
}
```

Module: `plugin/wrapper/scripts/groklib/implementation_contract.py`.

#### Contract rules (locked)

| Rule | Behavior |
|------|----------|
| `schemaVersion` | Must equal `1` |
| `taskId` | Stable restricted id: `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` |
| `objective` | Non-empty string; supplied to Grok; not wrapper-proved |
| `target` | Must match CLI `--target` after canonical normalization |
| Write-scope paths | Repository-relative; reject absolute paths, `..`, empty, NUL bytes, symlink escapes |
| `kind: "file"` | Matches exactly one path (normalized path components) |
| `kind: "subtree"` | Matches that directory and descendants using **path-component** semantics, not string-prefix matching (e.g. scope `a` does **not** match path `ab`) |
| Empty `writeScopes` | Invalid for code mode when contract is present |
| Enforcement timing | Wrapper enforces write scopes **after** Grok exits (changed paths vs scopes) |
| OS sandbox | Wrapper **may** also use write scopes to narrow the OS sandbox where supported; post-exit check remains mandatory |
| `acceptanceCriteria` | Supplied to Grok and included in handoff; wrapper does **not** claim semantic proof from prose |
| `requiredValidation` | `argv` arrays only; never execute through a shell; each entry needs non-empty `argv` (string list), relative `cwd` without `..`, non-empty `purpose` |
| Bad contract | Fail before Grok with `implementation-contract-invalid` |
| Path outside scopes | `write-scope-violation` after Grok; `integration.ready` false |
| No contract file | Code runs as today; write-scope check uses existing worktree confinement only; `integration.ready` may still be true if all other gates pass |
| Contract hash | Persist SHA-256 of canonical contract bytes as `contractSha256` (null if no contract) |

Support multiple validation targets (multiple packages / workspaces). Do not
assume one `package.json` is sufficient for a cross-package task.

### 14.4 Prohibit unexpected commits

1. Record the exact resolved full base SHA as `baseRevision` **before** Grok runs.  
2. After Grok exits, require:

```text
git rev-parse HEAD == baseRevision
```

3. If HEAD changed:

| Action | Required |
|--------|----------|
| Error class | `unexpected-commit` |
| Worktree / branch | Preserve for inspection |
| `integration.ready` | `false` |
| Convert commit to handoff | **No** — do not silently use commits as the artifact source |
| Reset / rewrite worktree | **No** |
| `--allow-commits` | Out of PR4 (future explicit mode) |

### 14.5 Immutable complete Git patch

Module (required path):

```text
plugin/wrapper/scripts/groklib/implementation_handoff.py
```

After sentinel verification and write-scope validation, generate a complete
patch using a temporary Git index.

#### Algorithm (exact order)

1. Remove the exact wrapper-owned sentinel `.grok-run-<run-id>` after validating it.  
2. Create a temporary index inside the private run directory:
   `runs/<run-id>/artifacts/handoff.idx` (mode 0600, private dir 0700).  
3. Set `GIT_INDEX_FILE` **only** for artifact-generation commands.  
4. `git read-tree <baseRevision>`.  
5. `git add -A` against the isolated worktree using the temporary index.  
6. `resultTreeOid = git write-tree`.  
7. Generate:

```text
git diff --cached --binary --full-index --no-ext-diff <baseRevision>
```

8. Write `artifacts/implementation.patch` atomically (temp + `os.replace`), mode **0600**.  
9. Calculate and persist: patch SHA-256, byte length, `resultTreeOid`, contract SHA-256, `createdAtUtc`.  
10. Re-read the patch from disk and verify SHA-256 before reporting terminal success for ready.  
11. Bounded size: default **25 MiB** (`26214400` bytes). Env
    `GROK_HANDOFF_PATCH_MAX_BYTES` integer, clamp **1 MiB .. 100 MiB**. Exceed →
    `artifact-generation-failure`; **never truncate**.  
12. Run the existing secret-material detector over the patch before exposing it
    as a handoff. Secret-shaped material fails closed with existing secret
    classification (`secret-material`).  
13. Ignored files remain excluded. Sentinel and temporary artifact files must
    **never** enter the patch.

#### Capture requirements

The patch **must** capture: modified, new untracked, deleted, renames, binary,
symlinks, executable-bit changes.

#### Source of truth

Do **not** treat the live worktree as the durable handoff after completion.
The **persisted patch** (plus handoff JSON) is the immutable source of truth.
Forensic inspection of the retained worktree remains allowed and useful.

#### Required artifact files

```text
runs/<run-id>/artifacts/implementation.patch
runs/<run-id>/artifacts/implementation-handoff.json
```

Optional private full command logs: `runs/<run-id>/artifacts/commands/` (mode 0600 files).

### 14.6 Implementation handoff schema (canonical)

One schema only. File: `implementation-handoff.json`.

```json
{
  "schemaVersion": 1,
  "runId": "20260716T020408Z-a82843",
  "taskId": "voice-policy-shared-contract",
  "contractSha256": "…",
  "baseRevision": "<full SHA>",
  "resultTreeOid": "<Git tree OID>",
  "changedFiles": [
    {
      "path": "packages/sharedSchemas/src/imagibooks/imagiexplainAdminPreview.ts",
      "status": "added",
      "oldPath": null
    }
  ],
  "patch": {
    "format": "git-binary-full-index-v1",
    "relativePath": "artifacts/implementation.patch",
    "sha256": "…",
    "bytes": 12345
  },
  "validation": {
    "requiredCommandsPassed": true,
    "buildGatePassed": true,
    "allPassed": true
  },
  "integration": {
    "ready": true,
    "blockers": []
  },
  "worktree": {
    "retained": true,
    "path": "<runtime path>",
    "branch": "grok/code/<run-id>"
  },
  "createdAtUtc": "…"
}
```

#### Field rules

| Field | Rule |
|-------|------|
| `taskId` / `contractSha256` | `null` if no contract file |
| `changedFiles[].status` | Git-style: `added`, `modified`, `deleted`, `renamed`, `typechange` as applicable |
| `changedFiles[].oldPath` | Set for renames; else `null` |
| `patch.format` | Exact string `git-binary-full-index-v1` |
| Alignment with C4 | Prefer single source: handoff may mirror `baseRevision` / `changedFiles` / worktree from the same post-Grok computation used for the envelope; do not maintain divergent independent copies |

### 14.7 `integration.ready` (wrapper-computed only)

`integration.ready` is computed by the wrapper, **never** by Grok prose.

It may be `true` **only** when all of the following hold:

1. Lifecycle is `completed`.  
2. HEAD still equals the exact full `baseRevision`.  
3. All changed files are inside declared write scopes (when contract present).  
4. No unexpected original-checkout write occurred.  
5. Sentinel verification passed.  
6. Patch generation and hash verification passed.  
7. All `requiredValidation` commands exited 0 (when contract present; if no contract, this clause is N/A and treated as pass for the clause).  
8. Existing wrapper-controlled build gate passed.  
9. Cleanup/auth/sandbox verification passed as required by existing code mode.  
10. No classified blocker remains (`blockers` is empty).  
11. At least one changed path exists (empty diff is **not** ready; blocker `no-changes`).

A failed implementation may still retain a **forensic** patch and handoff JSON,
but `integration.ready` must be `false` and `blockers` must name every reason
(e.g. `write-scope-violation`, `unexpected-commit`, `validation-failure`,
`secret-material`, `artifact-generation-failure`, `no-changes`, build-gate failure class).

### 14.8 Command validation evidence

Current command records already have argv, cwd, purpose, duration, exit status.
Extend each command record with:

```json
{
  "stdoutSha256": "…",
  "stderrSha256": "…",
  "stdoutTail": "…",
  "stderrTail": "…",
  "stdoutTruncated": true,
  "stderrTruncated": false
}
```

| Rule | Locked |
|------|--------|
| Hash | SHA-256 of complete captured stdout/stderr (before tail truncation) |
| Tail size | Max **4096** UTF-8 bytes each (or byte-bounded equivalent if non-UTF-8 safe decode) |
| Redaction | Apply existing secret redaction **before** persisting or returning tails |
| Full logs | Optional under `artifacts/commands/` mode 0600; never on the one-envelope stdout channel |
| Authority | Wrapper-controlled build-gate commands remain authoritative for the gate |
| Parent duty | Parent harness must re-run relevant validation after integration; task commands are evidence only |

### 14.9 Mode `/grok:handoff`

#### New files

```text
plugin/skills/handoff/SKILL.md
plugin/skills/handoff/run.mjs
plugin/wrapper/scripts/groklib/modes/handoff.py
```

Register `handoff` in wrapper modes, envelope MODES, and companion
`WRAPPER_MODES` / streaming lists as required for parity.

#### Behavior (strictly read-only)

1. Accept exactly one canonical `--run-id`.  
2. Verify run ownership.  
3. Require a terminal run (terminal lifecycle or valid terminal envelope).  
4. Load `implementation-handoff.json`.  
5. Re-hash `implementation.patch` and compare to stored `patch.sha256`.  
6. Validate handoff schema (`schemaVersion`, required keys, types).  
7. Return **one** JSON envelope including `integration.ready` and `blockers`.  
8. Never apply, commit, merge, push, or clean anything.  
9. Never modify the worktree.  
10. Fail `artifact-integrity-failure` if stored artifacts were modified (hash mismatch or schema break after load).  
11. Fail `handoff-unavailable` for runs that never produced an implementation artifact (non-code modes, interrupted before artifact, missing files).  

Durable identifier: **run ID**. Companion job ID may remain a UI convenience for
status/result but cannot be required for handoff.

### 14.10 Parent-harness integration protocol (document only)

Codex/Claude parent protocol — **document in skills/docs**; no auto-apply command in PR4:

1. Dispatch `code` from an exact committed base SHA.  
2. Use disjoint write scopes for parallel implementation peers.  
3. Wait for terminal lifecycle (`status` until not running).  
4. Read `/grok:handoff --run-id <id>`.  
5. Require `integration.ready === true`.  
6. Verify the patch hash (local re-hash or trust handoff mode re-verify).  
7. Inspect the patch semantically.  
8. Check that the parent branch still contains the recorded `baseRevision`.  
9. Check for dirty changes overlapping the patch paths.  
10. Run `git apply --check --binary <patch>`.  
11. Apply only after the parent agent or user **explicitly** authorizes integration.  
12. Never auto-commit or auto-push.  
13. Rerun affected tests and build gates in the parent worktree.  
14. Record the Grok `runId` and patch hash in execution evidence.  

Safe automatic application is a **separate** future feature after dogfooding.

### 14.11 Parallel peer-agent rules (document)

#### Suitable tasks

- One module or cohesive package boundary  
- Exact write paths or subtrees  
- Concrete acceptance criteria  
- Deterministic validation commands  
- No unresolved architecture decision  
- No production deployment or external write  
- No database migration unless separately designed and operator-approved  

#### Unsuitable tasks

- Broad repository refactors with overlapping ownership  
- Changes requiring continuous shared conversational context  
- Tasks whose write scope cannot be enumerated  
- Production migrations, deployment, or destructive operations  
- Multiple agents modifying the same paths concurrently  

#### Parallelism

- Parallel runs **must** have disjoint write scopes.  
- If task B depends on task A: integrate A first, dispatch B from the **new**
  committed revision. Do not dispatch dependent runs from the same stale base.  
- Parent Codex/Claude owns orchestration, integration order, final validation,
  and conflict resolution.

### 14.12 Cleanup and forensic behavior

| Rule | Locked |
|------|--------|
| Code worktrees | Retained by default (unchanged) |
| Artifact location | Run directory `artifacts/`, not only the worktree |
| Ownership | Cleanup continues to verify run/worktree ownership |
| Cross-run | Never remove another run’s worktree or branch |
| Ready handoff | Warn clearly when deleting a run that has `integration.ready: true` and no acknowledgment field; still allow explicit confirm |
| Claims | Never claim that cleanup integrated the implementation |
| Confirmation | Preserve existing explicit confirmation requirements |
| Auto-cleanup after success | **Do not** add; would remove the easiest forensic surface before parent inspection |

### 14.13 Error classes

#### New (add to `ERROR_CLASSES` and docs)

```text
implementation-contract-invalid
write-scope-violation
unexpected-commit
artifact-generation-failure
artifact-integrity-failure
handoff-unavailable
```

#### Reuse existing

```text
validation-failure
secret-material
wrong-working-directory
worktree-failure
state-ownership-violation
cleanup-failure
```

Every failure must produce a terminal envelope when safe persistence is still
possible. Forensic handoff may still be written with `integration.ready: false`
when partial artifacts exist.

### 14.14 Test matrix (mandatory)

Unit and integration tests must cover:

| Area | Cases |
|------|--------|
| Contract parsing | Strict schema; schemaVersion; taskId charset |
| Scopes | Exact file vs subtree; path-component not string prefix |
| Path rejection | Traversal, absolute, symlink escape, empty, NUL |
| Patch shapes | Modified, added, deleted, renamed, binary, symlink, executable-bit |
| Untracked | Previously untracked files included in patch |
| Ignored | Ignored files excluded |
| Sentinel | Grok sentinel excluded from patch |
| Commits | Unexpected Grok commit → `unexpected-commit`, ready false, no reset |
| Apply fidelity | Patch applies cleanly to exact base; applied tree matches `resultTreeOid` |
| Integrity | Patch tampering → handoff `artifact-integrity-failure`; contract tampering → hash fail |
| Scopes fail | Write-scope violation → not integration-ready |
| Gates | Build-gate failure retains forensic artifact, ready false; required-validation failure blocks ready |
| Empty | No-change code run not integration-ready (`no-changes`) |
| Interrupted | Handoff unavailable |
| Identity | Job ID not required when run ID known |
| Mode | Handoff mode is read-only (no worktree/git mutations in tests) |
| Paths | Spaces and non-ASCII path components handled safely |
| Concurrency | Concurrent runs cannot read or clean each other’s artifacts |
| Permissions | Artifact files/dirs private (0600/0700) |
| Secrets | Secret-shaped patch content fails closed |
| Command tails | Bounded and redacted |
| Suites | Full Python and Node suites pass |

### 14.15 Documentation and dual-host parity

Update all behavior-owning documentation required by AGENTS.md:

| Document |
|----------|
| `README.md` |
| `CHANGELOG.md` |
| `docs/roadmap.md` |
| `docs/COMPATIBILITY.md` |
| `docs/RELEASE.md` (smoke steps) |
| `docs/PROVENANCE.md` (one line if needed) |
| `plugin/references/README.md` |
| `plugin/references/manual-smoke.md` |
| `plugin/wrapper/references/authority-policies.md` |
| `plugin/wrapper/SKILL.md` |
| `plugin/skills/code/SKILL.md` |
| `plugin/skills/handoff/SKILL.md` (new) |
| Claude and Codex plugin/agent manifests / packaging version **1.6.0** |

Document explicitly:

- `/grok:transfer` transfers conversation context.  
- `/grok:handoff` transfers implementation output.  
- Neither command integrates code automatically.  
- Codex and Claude consume the same handoff schema.  
- The wrapper remains the sole authority for safety and readiness.  
- Notifications indicate terminal availability, not integration success.  

### 14.16 PR4 release

Treat PR4 as a **minor** release (**1.6.0** after 1.5.x) because it adds a
public handoff mode and schema.

#### Required automated evidence

```bash
cd plugin/wrapper/scripts
python3 -m unittest discover -s tests -q

cd plugin/scripts
node --test tests/*.test.mjs

claude plugin validate ./plugin --strict
```

#### Dual-host smoke

1. Claude Code: `code` → `status` → `handoff`.  
2. Codex: `code` → `status` → `handoff`.  
3. Failed code run → forensic handoff with `integration.ready: false`.  
4. Tampered patch → handoff integrity failure.  
5. Explicit cleanup after handoff inspection.  

## 15. Success criteria (full program)

- [ ] PR1–PR3 criteria (lifecycle, isolation, notify)  
- [ ] Code handoff artifacts + ready gate per §14.7  
- [ ] `/grok:handoff` integrity per §14.9  
- [ ] Dual-host smoke code→status→handoff  
- [ ] Parent protocol and parallel rules documented  
- [ ] No TBD/placeholder decisions remain in this design  

## 16. Out of scope

- Host chat completion APIs  
- Untracked under review `--isolated`  
- Windows native notify  
- Ignore-list review safety  
- Auto-apply handoff  
- `--allow-commits`  

