# Run lifecycle, isolated review, and completion signals

**Status:** design revision 3 (fully locked — no open decisions)  
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

## 3. Non-goals

- Chat injection into Claude or Codex from the wrapper or companion.
- Broad ignore lists as the read-only safety model.
- A second durable stream besides `progress.jsonl`.
- Failing a completed run because notification delivery failed.
- Applying untracked files under `--isolated` (v1).

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
| PR versions | PR1 **1.3.0**, PR2 **1.4.0**, PR3 **1.5.0**. |

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

| Class | Lifecycle when terminal |
|-------|-------------------------|
| `isolation-unavailable` | `failed` |
| `finalization-timeout` | `failed` |

Add both to `envelope.ERROR_CLASSES`.

## 13. Three PRs

| PR | Version | Scope |
|----|---------|--------|
| PR1 | 1.3.0 | Lifecycle, seed, persist, status projection, progress, process finalize |
| PR2 | 1.4.0 | Isolated review |
| PR3 | 1.5.0 | Notifications |

## 14. Success criteria

- [ ] No run id without seed record (`lifecycle: created`, `status: running`)  
- [ ] Status projection matches §6  
- [ ] Explicit lifecycle on every terminal persist  
- [ ] Finalize hang → `finalization-timeout`  
- [ ] `--base` / `--isolated` never silent live fallback  
- [ ] Notify default off; at-most-once; safe spawn  
- [ ] Docs lists complete on each PR  

## 15. Out of scope

- Host chat completion APIs  
- Untracked files in `--isolated`  
- Windows native toasts (v1 no-op)  
- Ignore-list review safety  
