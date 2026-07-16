# Run lifecycle, isolated review, completion signals, and implementation handoff

**Status:** design revision 8 (PR1–PR4 locked; rev-7 consistency fixes closed — no open decisions)  
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
   hash, and integrate.

## 2. Goals

- Durable target lifecycle for every live run.
- Atomic seed `run.json` before the run id is published.
- Atomic, validated terminal `envelope.json` before a run is finished.
- **Single terminal-envelope writer** with CAS/`recordRevision` (no lost races).
- Status projection per §6; **`status` remains strictly read-only** (no writes).
- Phase progress with process-local monotonic `elapsedMs` and UTC timestamps.
- Process-based finalization watchdog with fully specified worker protocol.
- Isolated review for `--base` and `--isolated`; ownership-marked worktrees; fail closed.
- Optional notifications after terminal envelope; **at-most-once attempt** (no duplicate auto-retry); never fail the run.
- Dual-host: same core; harnesses only present.
- Docs follow code on every shippable PR (AGENTS.md rule #1).
- Verified immutable `code` handoff: contract scopes, unexpected-commit check,
  operator-trusted contract validation execution, two-phase handoff readiness,
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
- Status mode writing lifecycle or any run-directory bytes.
- Automatic retry of failed/crashed notification attempts (operator-driven future PR).
- Multi-workspace authoritative build gates in one `code` run (PR4 = one target).

## 4. Locked decisions

| Topic | Decision |
|-------|----------|
| Lifecycle representation | `run.json.lifecycle` is durable truth for **persisted** state. Status may **derive** a display lifecycle (e.g. `interrupted`) without persisting it. Top-level envelope `status` is a projection (§6). |
| Seed record | `lifecycle: "created"`, compatible `status: "running"`, `recordRevision: 0`. |
| Status mode | **Strictly read-only.** Never writes under the target run directory. Byte-for-byte unchanged after every status query (test-enforced). |
| Interrupted durable write | **Not from status.** Status only **derives** display lifecycle (§6.3). |
| Terminal writer roles | **Worker** = normal terminal writer. **Parent** = recovery writer only when `proc.is_alive() is False` (confirmed), then re-read under lock (§9.4). Timed `join()` alone is **not** proof of death. All terminal writes go through `persist_terminal_envelope` CAS API only. |
| Envelope vs lifecycle crash | **Envelope-first** under lock; then lifecycle. Idempotent finish if matching valid envelope already exists. Status derives effective terminal lifecycle from valid envelope when record is still non-terminal (§7.1). |
| CAS | `run.json.recordRevision` monotonic integer; every lifecycle/record mutation requires expected revision; lock file §7. |
| Terminal immutability | A valid terminal envelope is **never** replaced by another terminal envelope. Terminal lifecycle never overwritten once set (except CAS completing non-terminal → terminal matching existing envelope). |
| Progress elapsed | Owning process uses **monotonic** clock for `elapsedMs` on events it writes. Cross-process/status uses UTC timestamps; clamp negative elapsed to 0. |
| Finalization | Child via `multiprocessing.get_context("spawn")`; fully specified payload and ownership (§9). |
| Review isolation | §10; ownership markers; no silent live-checkout fallback. |
| Notifications storage | **Only** jobs index `config` in `plugin/scripts/lib/jobs.mjs`. Never gate-state. |
| Notifications default | `off`. Setup recommends `auto`. |
| Notify contract | **At-most-once attempt:** prioritize no duplicate attempts over guaranteed delivery. **Not** exactly-once. No automatic retry of `pending`. |
| Background signal | Companion-only `GROK_COMPANION_EXECUTION_CONTEXT=foreground\|background` set by skill/agent shell prefixes; `skill-run.mjs` unchanged; never forwarded to wrapper; never inferred from TTY. |
| Handoff mode | `WRAPPER_MODES` only; **not** `STREAMING_MODES`; `runHandoff()` like status passthrough. |
| Contract validation | Explicit execution after scopes + HEAD; **operator-trusted argv, not OS-filesystem-sandboxed** (§14.3, §14.9). |
| Handoff readiness | Computed from in-memory `terminalOutcome`, not persisted lifecycle. Manifest then envelope. `/grok:handoff` requires ready manifest **and** completed envelope (§14.6–14.12, §14.14). |
| Code target scope | **One** cohesive `--target` workspace per `code` run in PR4. |
| PR versions | PR1 **1.3.0**, PR2 **1.4.0**, PR3 **1.5.0**, PR4 **1.6.0** (four minor releases for independent dogfood). |
| Packaging version paths | Exactly three: `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`, `.claude-plugin/marketplace.json`. |
| PROVENANCE | No content change required for this program unless a release note needs a one-line D-log entry; **locked: no `docs/PROVENANCE.md` edit in PR1–PR4** unless a finding forces it. |

## 5. Architecture

```text
Companion → Wrapper → state_root/runs/<id>/
  run.json          # CAS + recordRevision + lifecycle
  run.lock          # exclusive lock for record/envelope mutations
  progress.jsonl
  envelope.json     # terminal only; never replaced once valid
  owner.json, owner.pid
  notified.json     # companion notify marker (attempt, not delivery guarantee)
  finalize-payload.json
  finalize-result.json
  finalize-worker.stderr
  artifacts/        # PR4 handoff (code)
```

Worktrees:

- Code/verify: existing external worktree paths + ownership (unchanged).
- Review isolation: `{state_root}/worktrees/review/{run_id}/` + owner marker bound to run id (§10).

## 6. Lifecycle and status projection

### Lifecycle values on `run.json.lifecycle` (persisted)

| Value | Meaning |
|-------|---------|
| `created` | Seed written; run id may be published |
| `running` | Active work before post-model finalize |
| `finalizing` | After Grok exit; packaging terminal result |
| `completed` | Valid success envelope persisted by terminal writer |
| `failed` | Valid failure envelope persisted by terminal writer |
| `canceled` | Operator cancel with terminal envelope |

**Not persisted by status:** `interrupted` is a **derived** display lifecycle only (§6.3).

### Transition graph (allowed **persisted** writes under CAS)

```text
created → running | failed | canceled
running → finalizing | failed | canceled
finalizing → completed | failed | canceled
```

Terminal persisted lifecycles `completed`, `failed`, `canceled` are **immutable**.
Further CAS transitions that would overwrite them **fail closed** (no write).

### Effective lifecycle resolution (read path, status and handoff)

Compute **in memory** (never write from status):

1. If `run.json.lifecycle` is terminal (`completed` | `failed` | `canceled`) → use it; `lifecycleSource: "record"`.  
2. Else if a **valid** stored terminal envelope exists → derive from envelope:  
   - envelope top-level `status == "success"` → effective lifecycle `completed`  
   - else → effective lifecycle `failed`  
   - `lifecycleSource: "envelope"` (crash-recovery display: envelope written, lifecycle not yet).  
3. Else if owner provably dead and lifecycle non-terminal → effective `interrupted`; `lifecycleSource: "derived"`.  
4. Else → use persisted non-terminal lifecycle; `lifecycleSource: "record"`.

### Top-level status projection (status mode and status-shaped envelopes)

| Effective lifecycle | Top-level `status` | Status-mode exit |
|---------------------|--------------------|------------------|
| `created`, `running`, `finalizing` | `running` | 0 |
| `completed` | `success` | 0 |
| `failed`, `canceled` | `failure` | 1 |
| derived `interrupted` | `failure` | 1 |
| Cannot load/own run; stored envelope unreadable or invalid C4 | `failure` | 1 |

### 6.3 Status is strictly read-only

`status` mode:

1. Loads run record and optional envelope **without** opening the run for write.
2. Never creates, truncates, renames, or chmods files under the target run directory.
3. Never calls `set_lifecycle`, CAS mutators, or recovery writers.
4. Derives display fields only in memory using §6 effective lifecycle resolution.

**Derived interrupted rule** (display only): step 3 of effective lifecycle resolution.  
**Do not** persist `interrupted` into `run.json`.

**Crash window (envelope present, record non-terminal):** status reports effective terminal lifecycle from the envelope (§6 step 2). Status still writes nothing. Durable lifecycle completion is the recovery path in §7.1 / §9.4.

**Test:** after every status query, the target run directory is **byte-for-byte unchanged**.

**Skill / harness:** `plugin/skills/status/SKILL.md` must state that exit 1 can mean “successfully inspected a failed/interrupted **target**,” not “status command malfunctioned.” Harness must relay the JSON envelope regardless of exit status. Distinguish envelope parse failure (tool broken) from target failure (useful envelope present).

### Seed `run.json` (exact fields)

```json
{
  "schemaVersion": 1,
  "runId": "<id>",
  "mode": "<mode>",
  "createdAtUtc": "<iso-z>",
  "lifecycle": "created",
  "status": "running",
  "recordRevision": 0,
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

### `create_run` callers (complete inventory)

Production:

| Caller | File |
|--------|------|
| Shared live modes (review, reason, …) | `plugin/wrapper/scripts/groklib/modes/_shared.py` |
| Worktree modes (code, verify) | `plugin/wrapper/scripts/groklib/modes/_worktree.py` |
| Preflight | `plugin/wrapper/scripts/groklib/modes/preflight.py` |

Tests (must update fixtures for seed + CAS):

- `plugin/wrapper/scripts/tests/test_runstate.py` (**exists** — not new)
- `plugin/wrapper/scripts/tests/test_mode_status.py`
- `plugin/wrapper/scripts/tests/test_mode_cleanup.py`
- `plugin/wrapper/scripts/tests/test_mode_review.py`
- any other fixture that calls `create_run` or dumps full `run.json`

### Record write semantics (single model — no mixed APIs)

| API | Semantics |
|-----|-----------|
| `create_run` | Creates run dir, owner marker, **seed `run.json`**, then `emit_run_id_marker`. Seed is the first write. |
| `cas_update_run_record(paths, expected_revision, patch)` | **Only** mutator for non-terminal record fields. Under exclusive lock: load, require `recordRevision == expected_revision`, apply **validated merge patch**, set `recordRevision = expected + 1`, atomic write. Reject unknown keys and lifecycle fields unless caller is `set_lifecycle` / terminal path. |
| `set_lifecycle(paths, expected_revision, lifecycle)` | CAS graph transition only. |
| `persist_terminal_envelope(paths, expected_revision, envelope, *, lifecycle)` | Single CAS terminal API (§7.1). Envelope-first; idempotent lifecycle finish. |
| `write_run_record` (public) | **Deleted** after all call sites migrate in PR1. Internal helper `_write_run_json_unlocked` may exist only for CAS paths under lock. No public full-replacement API. |

Later field updates (model, repository, worktree paths, etc.) use `cas_update_run_record` merge patches only. Merge **preserves** `runId`, `createdAtUtc`, and never silently resets `lifecycle` or `recordRevision`.

**Call-site migration (mandatory in PR1):** every current `write_run_record` / `best_effort_write_run_record` site in `_shared.py`, `_worktree.py`, `preflight.py`, and tests must move to CAS APIs. Terminal paths use only `persist_terminal_envelope`.

## 7. Durable invariants

1. `[grok-run-id]` is emitted only after atomic seed `run.json` exists.  
2. Terminal publication uses `persist_terminal_envelope` only (§7.1).  
3. `persist_terminal_envelope` always receives explicit terminal lifecycle + expected revision (or idempotent complete-from-existing path).  
4. JSON writes: temp sibling `path.name + ".tmp." + pid` then `os.replace`, file mode 0600.  
5. Progress: only `progress.jsonl` (append).  
6. **Lock:** exclusive `run.lock` (`fcntl` on Unix, `msvcrt` on Windows) held for every CAS record or envelope mutation.  
7. Terminal envelope never replaced once validated.  

### 7.1 Crash-consistent terminal persistence (exact)

Under `run.lock`, `persist_terminal_envelope` performs:

```text
1. Load run.json; verify expected_revision (unless idempotent-complete path).
2. If a valid terminal envelope.json already exists:
   a. Refuse to replace it with a different envelope body.
   b. If run.json.lifecycle is still non-terminal: CAS lifecycle to the lifecycle
      implied by the existing envelope (success→completed, else→failed) and
      bump recordRevision. Return success (idempotent finish).
   c. If lifecycle already terminal and matches envelope: no-op success.
   d. If lifecycle terminal conflicts with envelope class: fail closed (corrupt state).
3. Else (no valid envelope yet):
   a. Validate the new envelope object.
   b. Write envelope.json atomically (FIRST).
   c. CAS lifecycle to completed|failed|canceled (SECOND).
   d. Bump recordRevision.
```

**Crash between 3b and 3c:** disk has valid envelope + non-terminal lifecycle.  
- Status/handoff **read path** uses effective lifecycle from envelope (§6).  
- Next **authorized recovery writer** (§9.4) must call `persist_terminal_envelope` (or an internal `complete_lifecycle_from_envelope` used only by that API) to finish step 2b.  
- Tests: simulate crash after envelope write before lifecycle write; assert status derives completed/failed; assert recovery call finishes lifecycle; assert second envelope write cannot replace.

### 7.2 Authorized recovery writers (durable lifecycle only)

| Writer | When allowed | What it may write |
|--------|--------------|-------------------|
| Finalization **worker** | Normal path | Any terminal envelope for the mode outcome via `persist_terminal_envelope` |
| Finalization **parent** | Only after worker process is proven not running (exited or kill attempted), `join` returned, re-read under lock | (1) Idempotent lifecycle completion for existing valid envelope; (2) **new** failure envelope only if no valid envelope yet, and only classes in §9.4 |

Status, handoff, cleanup dry-run: **never** recovery writers.

## 8. Progress

### Phases (exact order vocabulary)

`start` | `validate` | `authhome` | `prepare` | `grok` | `finalizing` | `notify` | `done`

`notify` is companion-side only (optional); wrapper may omit it.

### Event fields

Every event: `schemaVersion`, `runId`, `seq`, `ts` (UTC ISO-Z), `phase`, `level`, `message`, plus `elapsedMs` (int).

### Elapsed time (exact)

| Writer | Rule |
|--------|------|
| Parent process ProgressWriter | Capture `time.monotonic()` at first emit after seed; `elapsedMs = int((monotonic_now - start) * 1000)`. |
| Spawned finalize worker | **Does not write progress.jsonl.** Parent owns progress around join. |
| Status mode display | Compute from `createdAtUtc` vs wall clock UTC; if negative or unparsable, use `0`. Prefer last event’s stored `elapsedMs` for `lastEvent`. |

Do **not** pass monotonic start values across processes (clock domains are not guaranteed).

### Status `response.target` (exact keys)

```json
{
  "lifecycle": "finalizing",
  "lifecycleSource": "record",
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

- `lifecycleSource`: `"record"` | `"envelope"` | `"derived"`.  
- `recentEvents`: last **8** events as compact summaries.  
- `resultAvailable`: true iff valid stored envelope exists.

## 9. Finalization watchdog (fully specified protocol)

### Who owns what

| Responsibility | Owner |
|----------------|-------|
| Set lifecycle `finalizing` (CAS) | Parent, before spawn |
| Progress enter/timeout/success around join | Parent only |
| Auth-home cleanup | **Worker** (same as today’s post-Grok finalize path moved into worker) |
| Sandbox verify / drift / mode envelope build | Worker |
| Normal terminal envelope + lifecycle | **Worker** via `persist_terminal_envelope` |
| Parent recovery terminal writes | **Parent only** under §9.4 after worker dead+joined+reread |
| progress.jsonl during finalize | Parent only |

### Worker input (`finalize-payload.json`, mode 0600)

Exact serializable JSON only — **no** open handles, locks, picklable closures, or live context objects. Process target is `finalize_worker_main(payload_path: str)` reading this file.

```json
{
  "schemaVersion": 1,
  "runId": "<id>",
  "mode": "review|reason|code|verify|…",
  "runDir": "<absolute>",
  "expectedRecordRevision": 3,
  "baseRevision": "<full sha or null>",
  "worktreePath": "<absolute or null>",
  "privateHomePath": "<absolute>",
  "repoRoot": "<absolute or null>",
  "targetWorkspace": "<absolute or null>",
  "resultPath": "<absolute runDir/finalize-result.json>",
  "stderrPath": "<absolute runDir/finalize-worker.stderr>",
  "modeContext": {}
}
```

`modeContext` holds only JSON-serializable scalars/lists/dicts needed to rebuild the mode’s success/failure envelope (command summaries, warning lists already collected, paths as strings). Parent freezes modeContext **before** spawn.

### Worker output (`finalize-result.json`)

```json
{
  "schemaVersion": 1,
  "ok": true,
  "lifecycle": "completed",
  "envelopePath": "<absolute>",
  "errorClass": null,
  "message": null,
  "recordRevisionAfter": 5
}
```

On failure without envelope: `ok: false`, `errorClass`, `message`.  
On success: envelope already persisted; `ok: true`.

### Parent sequence (exact)

1. CAS lifecycle → `finalizing`; progress `"entering finalization"`.  
2. Write `finalize-payload.json` (0600).  
3. `ctx = multiprocessing.get_context("spawn")`; `Process(target=finalize_worker_main, args=(payload_path,), name="grok-finalize")`.  
4. `proc.start()`; `proc.join(timeout=budget_seconds)`.  
5. If `proc.is_alive()`:  
   - `proc.terminate()`; `proc.join(5)` (terminate grace **5s**).  
   - If still alive: `proc.kill()`; `proc.join(5)` (kill grace **5s**).  
6. **Liveness gate (mandatory):** re-check `proc.is_alive()`.  
   - If **alive**: parent recovery writes are **forbidden**. Progress `"finalization worker unkillable"`. Do **not** call `persist_terminal_envelope`. Do **not** write `envelope.json` or change lifecycle. Return an **ephemeral** (stdout-only, not run-dir) failure envelope with class `finalization-worker-unkillable` so the operator sees failure this session; durable state remains `finalizing` until the worker exits and either writes a terminal envelope or the owner dies (status then derives `interrupted` if no envelope).  
   - If **not alive**: enter **parent recovery** (§9.4).

**Timed `join()` returning is never treated as proof of death.** Only `proc.is_alive() is False` authorizes parent recovery.

### 9.4 Parent recovery writer (exact authority)

**Preconditions (all required):**

1. **`proc.is_alive() is False`** (confirmed after any terminate/kill sequence).  
2. Exclusive `run.lock` acquired.  
3. Fresh re-read of `envelope.json` + `run.json`.

If precondition 1 fails → §9 step 6 unkillable path; no durable parent write.

**Actions under lock (ordered), only when preconditions hold:**

1. If valid terminal envelope exists:  
   - Call `persist_terminal_envelope` idempotent path to finish lifecycle if needed.  
   - **Never** replace the envelope.  
   - Return that envelope to the parent caller.  
2. Else if worker exit code was 0 but no envelope: parent may write **one** new failure envelope via `persist_terminal_envelope` with class `finalization-worker-missing-result`, lifecycle `failed`.  
3. Else if the budget timed out (join hit timeout) and no envelope: parent may write **one** new failure envelope with class `finalization-timeout`, lifecycle `failed`.  
4. Else if worker exited non-zero without envelope: parent may write **one** new failure envelope with class `cli-failure` (detail from `finalize-worker.stderr` / finalize-result if present), lifecycle `failed`.  
5. No other parent-authored error classes. No parent success envelopes. **No parent durable writes while worker is alive.**

**Parent-authorized new failure classes for durable envelopes (complete list):**

```text
finalization-timeout
cli-failure
finalization-worker-missing-result
```

**Ephemeral-only (stdout, not persisted under run dir):**

```text
finalization-worker-unkillable
```

All durable terminal writes through `persist_terminal_envelope` only.

### Budgets

| Modes | Seconds |
|-------|---------|
| review, reason, adversarial-review (maps to review) | 120 |
| code, verify | 180 |
| preflight, status, cleanup, handoff | no finalize worker |

Env `GROK_FINALIZE_TIMEOUT_SECONDS`: integer, clamp **30..600**, overrides table when set.

### Tests (mandatory)

- Worker completion immediately **before** parent timeout path.  
- Worker completion **during** timeout/kill window.  
- Worker completion **after** kill attempt (envelope preserved).  
- Terminal envelope never replaced by another terminal envelope.  
- Crash after envelope write before lifecycle write: status derives terminal; recovery finishes lifecycle; envelope body unchanged.  
- Spawn works on macOS; Windows spawn tested when CI/platform available (skip with explicit marker only if platform lacks spawn — prefer run).  
- No non-serializable objects in payload.  
- Parent cannot write success envelope.  
- Parent cannot durable-write while `proc.is_alive()` (unit test of guard).  
- After kill grace still alive → no durable timeout envelope; ephemeral `finalization-worker-unkillable`; lifecycle remains `finalizing`.

## 10. Isolated review

### When

| Flags | Action |
|-------|--------|
| `--base` set | Isolation required |
| `--isolated` set | Isolation required |
| both | Worktree at HEAD; apply tracked dirty; keep `--base` for comparison |
| neither | Live checkout; drift warnings only |

### Worktree path and ownership

- Path: `{state_root}/worktrees/review/{run_id}` where `state_root = runstate.state_root()`.  
- Write owner marker bound to `run_id` (same C2 schema as code worktrees; path `…/review/{run_id}.owner.json` sibling or `owner.json` inside — **locked: sibling** `{worktree_path}.owner.json` matching code-mode pattern).  
- Never silently reuse an existing path: if path or git worktree registration exists, fail `isolation-unavailable`.  
- Partial setup: if directory or registration exists but init failed, cleanup attempt then fail closed.  

### Dirty patch rules (exact)

1. From **repository root** (git toplevel of original checkout):  

```text
git diff --binary --full-index --ita-invisible-in-index HEAD --
```

2. This combines **staged and unstaged tracked** modifications relative to HEAD.  
3. Ordinary untracked files: **never** included.  
4. **Intent-to-add** (`git add -N`): excluded by `--ita-invisible-in-index` (not treated as tracked additions in the isolation patch).  
5. Dirty submodules or unsupported gitlinks: **reject** with `isolation-unavailable` (PR2 does not support them).  
6. Write patch to `{worktree_path}.diff` mode 0600.  
7. If size > 0: `git -C {worktree_path} apply --whitespace=nowarn {diff_path}` from isolated repo root.  
8. Any patch-generation or apply failure → `isolation-unavailable` (cleanup first).  
9. Empty diff: continue.  
10. Preserve original `--base` for review comparison; isolation HEAD is **not** the comparison base.  
11. **Test:** `git add -N` file does not appear in isolation worktree after apply; staged+unstaged tracked edits do.

### Cleanup (always)

On success, classified failure, cancellation, and timeout:

1. `git -C repo_root worktree remove --force {worktree_path}`  
2. `git worktree prune` best-effort  
3. Remove marker and diff file  
4. `rmtree` worktree path if needed  
5. Failures log only; never delete another run’s worktree  

### Concurrent runs

Concurrent review isolations must not share paths; run-id uniqueness enforces this. Tests for concurrent-run and partial-cleanup.

## 11. Notifications

### Storage (exact)

Jobs index used by `plugin/scripts/lib/jobs.mjs`. Config always includes:

```json
{
  "runMode": "hardened",
  "notificationMode": "off",
  "notificationWebhookUrl": null
}
```

### Execution context signal (companion-only)

Environment variable on the **companion process** (not the wrapper):

```text
GROK_COMPANION_EXECUTION_CONTEXT=foreground|background
```

| Rule | Locked |
|------|--------|
| Who sets it | The host skill/agent invocation that chooses Wait vs background prefixes `GROK_COMPANION_EXECUTION_CONTEXT` in the shell environment **before** `node …/run.mjs` or `node …/agents/run.mjs`. **`plugin/scripts/lib/skill-run.mjs` has no functional change** in this program (no new helper, no env inference). |
| Forward to wrapper | **Never** (companion strips/ignores for wrapper argv and wrapper child env) |
| Infer from TTY | **Never** |
| Missing / invalid | Treat as `foreground` |

### Modes (exact)

| Mode | Behavior |
|------|----------|
| `off` | Never notify |
| `auto` | Notify only if execution context is **`background`** and a native channel is available |
| `native` | Attempt native for **foreground and background** when channel available |
| `webhook` | POST to URL if non-null non-empty; else no-op log |

### At-most-once attempt (exact contract)

**Priority: no duplicate attempts over guaranteed delivery.** Do not call this exactly-once delivery.

File: `{runDir}/notified.json`.

1. Atomically create exclusive marker:  
   `{"state":"pending","attemptedAt":"<iso>","adapter":null,"result":null}`  
2. If file already exists (any state): return `{attempted:false, sent:false, reason:"already-attempted"}` — **never auto-retry**.  
3. Perform external notification once.  
4. Overwrite marker:  
   `{"state":"completed","attemptedAt":"…","completedAt":"…","adapter":"native|webhook","result":"sent|failed","detail":"…"}`  
5. On send failure after create: still write `state: completed` with `result: failed` so automatic retry never re-fires.  
6. Crash after send before complete write: marker may remain `pending`; next process **must not** auto-retry (duplicate risk). Operator-driven retry is a **future PR**, not this program.  
7. Never throw to fail the job.

### Native adapters (exact)

| Platform | Command argv | Timeout |
|----------|--------------|---------|
| Darwin | `["osascript", "-e", "display notification \"" + escape(body) + "\" with title \"" + escape(title) + "\""]` | 5000 ms |
| Linux | `["notify-send", "--", title, body]` if on PATH | 5000 ms |
| Windows | no-op, reason `windows-native-unsupported` | n/a |
| Other | no-op | n/a |

Always `shell: false`. Title `Grok Skills`. Body `"{mode} {lifecycle} · {runId} · {durationSeconds}s"`.

### Webhook (exact)

- POST JSON `{"runId","mode","lifecycle","durationSeconds"}`  
- Timeout 3000 ms  
- URL from config only  

### When companion notifies

- After background job wrapper closes with terminal envelope/lifecycle, if mode is `auto` or `native` or `webhook`.  
- For `native`: also after foreground terminal.  
- Never on `status` / `result` / `jobs` / `setup` / `handoff` alone.  
- Tests for Claude and Codex skill paths setting both execution contexts.

## 12. Error classes

| Class | Lifecycle when terminal | PR |
|-------|-------------------------|-----|
| `isolation-unavailable` | `failed` | PR2 |
| `finalization-timeout` | `failed` | PR1 |
| `finalization-worker-missing-result` | `failed` | PR1 |
| `finalization-worker-unkillable` | ephemeral stdout only; lifecycle stays `finalizing` | PR1 |
| `implementation-contract-invalid` | `failed` | PR4 |
| `write-scope-violation` | `failed` | PR4 |
| `unexpected-commit` | `failed` | PR4 |
| `artifact-generation-failure` | `failed` | PR4 |
| `artifact-integrity-failure` | `failed` (handoff mode) | PR4 |
| `handoff-unavailable` | `failed` (handoff mode) | PR4 |
| `terminal-envelope-incomplete` | reported by handoff as not ready / failure class when applicable | PR4 |

Reuse existing: `validation-failure`, `secret-material`, `wrong-working-directory`, `worktree-failure`, `state-ownership-violation`, `cleanup-failure`, `cli-failure`.

Add new classes to `envelope.ERROR_CLASSES` in the owning PR.

## 13. Four PRs

| PR | Version | Scope |
|----|---------|--------|
| PR1 | 1.3.0 | Lifecycle, CAS seed, single terminal writer, status read-only projection, progress, process finalize |
| PR2 | 1.4.0 | Isolated review + ownership |
| PR3 | 1.5.0 | Notifications + execution context |
| PR4 | 1.6.0 | Verified implementation handoff |

Release choice: **four consecutive minor releases** for independent dogfood (accepted operational cost).

## 14. PR4 — Verified implementation handoff

### 14.1 Purpose

Make Grok `code` a peer implementer for Codex/Claude via verified immutable artifacts.
Parent reviews and integrates. PR4 never auto-commits, merges, cherry-picks, pushes, or edits the parent checkout.

| Command | Transfers | Key |
|---------|-----------|-----|
| `/grok:transfer` | Conversation context | session |
| `/grok:result` | Companion job output | job ID (UI) |
| `/grok:handoff` | Implementation output | **`runId` only** |

### 14.2 Grounding

| Component | Owns |
|-----------|------|
| `plugin/skills/code/SKILL.md` | Isolated external worktree, uncommitted, retained |
| `plugin/wrapper/scripts/groklib/modes/code.py` | Sentinel, diff confinement, deps, **single-target** build gate |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | code/verify lifecycle |
| `plugin/wrapper/scripts/groklib/worktree.py` | Worktree create, ownership, cleanup |
| C4 fields | `baseRevision`, `changedFiles`, `diffSummary`, `commands`, worktree metadata |

### 14.3 Contract file

Optional but recommended: `--contract-file <path>`. Keep `--task` / `--task-file`.

#### Trust model (locked — explicit, not OS-sandbox)

The contract is **trusted operator authority**: supplied by the parent harness or human operator who already has the right to run `code` on that repository. It is **not** untrusted model output.

**Filesystem sandboxing claim (locked):** contract `requiredValidation` commands are **not** OS-filesystem-sandboxed. Arbitrary argv under this trust model **can** write outside the worktree if the operator points them at a capable binary. PR4 does **not** claim or test a hard “cannot write outside the worktree” guarantee for those commands.

What **is** enforced:

| Control | Behavior |
|---------|----------|
| Shell | Never; argv array only (`shell=False` / no shell string) |
| `cwd` | Must resolve under the isolated worktree + single `--target`; reject escapes, absolute escapes, `..` |
| Original checkout | After each validation command, run existing original-checkout unmodified assertion; violation → blocker / fail closed for readiness |
| Worktree escape (code confinement) | Existing code-mode post-command / post-gate escape checks still apply to the Grok worktree and original checkout |
| Prefer package scripts | Prefer argv that invoke committed package-manager scripts for the target workspace; arbitrary argv remains allowed under operator trust |
| Parent duty | Parent always re-runs relevant validation after integration |

`validation.sources.contractRequiredValidation.trustModel` value (exact string):

```text
operator-contract-trusted-no-os-sandbox
```

#### Schema

```json
{
  "schemaVersion": 1,
  "taskId": "voice-policy-shared-contract",
  "objective": "Implement the shared ImagiExplain voice policy contract",
  "target": ".",
  "writeScopes": [
    { "path": "packages/sharedSchemas/src/imagibooks/imagiexplainAdminPreview.ts", "kind": "file" },
    { "path": "packages/sharedSchemas/src/imagibooks/imagiexplainAdminPreview.test.ts", "kind": "file" }
  ],
  "acceptanceCriteria": ["…"],
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

| Rule | Behavior |
|------|----------|
| schemaVersion | `1` |
| taskId | `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` |
| target | Match CLI target after canonical normalization |
| Paths | Repo-relative; reject absolute, `..`, empty, NUL, symlink escape |
| file / subtree | Exact vs path-component prefix (not string prefix) |
| Empty writeScopes | Invalid when contract present |
| Bad contract | `implementation-contract-invalid` **before** Grok |
| No contract | Existing confinement only |

### 14.4 One target workspace (PR4)

PR4 constrains each `code` run to **one** cohesive `--target` workspace.

- Wrapper-owned build gate remains the existing single-target gate for that workspace.  
- Contract validation commands must resolve `cwd` under that target/worktree; they do **not** replace the build gate.  
- Cross-package work spanning multiple independent package roots: sequential runs or declare all work under one target that legitimately owns the gate — not multi-root gates in PR4.

### 14.5 Unexpected commits

Before Grok: record full `baseRevision`. After Grok: `git rev-parse HEAD == baseRevision`.

If not: append blocker `unexpected-commit` (see §14.8); ready false; **preserve** worktree; **no** reset; continue forensic capture if state readable. Primary classified failure uses unexpected-commit when it is the primary policy failure.

### 14.6 Finalization order for code (exact)

```text
1. verify sentinel
2. remove exact sentinel only (never a user-authored similarly named path)
3. verify HEAD still equals baseRevision
4. compute changed files (sentinel must not appear)
5. enforce write scopes (contract)
6. capture forensic patch (phase 1) + provisional blockers
7. execute requiredValidation (operator-trusted; cwd confined; no OS FS sandbox claim)
8. execute wrapper build gate (authoritative)
9. complete shared safety checks (sandbox verify, auth-home cleanup, original-checkout)
10. decide in-memory terminalOutcome ∈ {completed, failed} and blockers
11. compute integration.ready from terminalOutcome + gates (§14.12) — NOT from persisted lifecycle
12. persist final handoff JSON (phase 2) with that ready/blockers snapshot
13. persist terminal envelope via persist_terminal_envelope (envelope-first, then lifecycle)
```

**Invariants for steps 10–13:**

- At step 11, persisted lifecycle is still `finalizing`; readiness must **not** read `run.json.lifecycle`.  
- `terminalOutcome === "completed"` means “this run will publish a success envelope,” not “lifecycle is already completed on disk.”  
- Crash after step 12 before step 13 completes: see §14.14 handoff observation rules.  
- Never rewrite an integration-ready manifest after a terminal envelope has been published.

Tests: sentinel never in changed files/patch; missing/symlinked/malformed sentinel fails; sentinel removal cannot delete user path with similar name; crashes between manifest / envelope / lifecycle.

### 14.7 Two-phase handoff artifacts

Module: `plugin/wrapper/scripts/groklib/implementation_handoff.py`.

**Phase 1 — immutable patch capture** (after scopes/HEAD; before/while remaining gates):

1. Temp index uniquely named under `artifacts/` (e.g. `handoff.<pid>.<token>.idx`).  
2. `GIT_INDEX_FILE` only for artifact git commands.  
3. `read-tree` base → `add -A` → `write-tree` → `diff --cached --binary --full-index --no-ext-diff`.  
4. Atomic `implementation.patch` mode 0600; SHA-256; re-read verify.  
5. Max 25 MiB default; env `GROK_HANDOFF_PATCH_MAX_BYTES` clamp 1–100 MiB; never truncate.  
6. Secret detector on patch; fail closed `secret-material`.  
7. **Temp-index cleanup (mechanical):** in `finally`, attempt delete of the temp index path.  
   - After delete attempt, **post-check** `path.exists()`:  
     - If path **still exists** → append blocker `temp-index-retained`; force `integration.ready` false (at phase-2 compute); log classified cleanup failure.  
     - If delete raised but post-check shows path **absent** → record a non-blocking warning only; do **not** set `temp-index-retained`.  
   - Temp index must never appear in the handoff patch.  
8. Record provisional blockers accumulated so far.

**Phase 2 — final handoff manifest** after steps 7–11 of §14.6 (all gates + `terminalOutcome` + ready compute), **before** terminal envelope:

- Write `implementation-handoff.json` once with `integration.ready` and complete `blockers` based on `terminalOutcome` (§14.12).  
- Failed runs may keep forensic patch; if `terminalOutcome === "failed"`, manifest is permanently `ready: false`.

### 14.8 Blocker accumulator (not raise-and-abort for policy)

Post-Grok **policy** failures use a blocker list:

| Kind | Behavior |
|------|----------|
| Integration blockers (unexpected-commit, write-scope-violation, validation-failure, no-changes, build-gate, secret-material when patch rejected, etc.) | Append blocker; ready false; **continue** forensic capture when repo readable |
| Unrecoverable (ownership failure, unsafe path, artifact corruption mid-write, unreadable git) | Abort capture; no ready; classified failure |

After capture: produce classified failure envelope using **primary** blocker (first hard policy failure in order of detection), with **all** blockers listed in handoff.

### 14.9 Execute contract validation (mandatory)

Dedicated step (plan Task 4.5):

1. Only after scope + unexpected-commit checks (and sentinel removal).  
2. For each `requiredValidation` entry: resolve `cwd` under isolated worktree; reject escapes.  
3. Execute argv array with `shell=False` — **no OS filesystem sandbox** (§14.3).  
4. After each command: original-checkout unmodified assertion (existing code-mode check).  
5. Record full command evidence (hashes + redacted tails) **before** interpreting exit status.  
6. Nonzero exit → blocker; prevents `integration.ready`.  
7. **Tests (match contract):** cwd escape rejected; shell never used; original-checkout write after validation fails readiness; do **not** claim or assert OS-level “cannot write outside worktree.”

### 14.10 Validation authority in handoff schema

```json
{
  "validation": {
    "requiredCommandsPassed": true,
    "buildGatePassed": true,
    "allPassed": true,
    "sources": {
      "wrapperBuildGate": { "authoritative": true, "passed": true },
      "contractRequiredValidation": {
        "authoritative": true,
        "passed": true,
        "trustModel": "operator-contract-trusted-no-os-sandbox"
      },
      "modelClaimedCommands": { "authoritative": false, "note": "ignored for readiness" }
    }
  }
}
```

- Wrapper build gate: authoritative.  
- Wrapper-executed contract validation: authoritative for exit-status evidence under operator-trust model only (not OS FS sandbox).  
- Grok-prose command claims: non-authoritative; never set readiness.  
- Parent always reruns relevant validation after integration.

### 14.11 Handoff JSON schema + single validator

Canonical validation function: `validate_implementation_handoff(doc: dict) -> list[str]` in `implementation_handoff.py`.

Writer and `modes/handoff.py` **must** call the same function. No separate hand-edited public JSON Schema file in PR4 (avoid dual sources). Round-trip writer-reader test prevents drift.

Full document shape:

```json
{
  "schemaVersion": 1,
  "runId": "20260716T020408Z-a82843",
  "taskId": "voice-policy-shared-contract",
  "contractSha256": "…",
  "baseRevision": "<full SHA>",
  "resultTreeOid": "<Git tree OID>",
  "changedFiles": [
    { "path": "…", "status": "added", "oldPath": null }
  ],
  "patch": {
    "format": "git-binary-full-index-v1",
    "relativePath": "artifacts/implementation.patch",
    "sha256": "…",
    "bytes": 12345
  },
  "validation": { "requiredCommandsPassed": true, "buildGatePassed": true, "allPassed": true, "sources": {} },
  "integration": { "ready": true, "blockers": [] },
  "worktree": { "retained": true, "path": "…", "branch": "grok/code/<run-id>" },
  "createdAtUtc": "…"
}
```

### 14.12 `integration.ready` (computed from `terminalOutcome`, not disk lifecycle)

Wrapper-computed only, using the in-memory decision from §14.6 step 10.

**Manifest write-time ready** may be true only when all hold:

1. `terminalOutcome === "completed"` (in memory; persisted lifecycle may still be `finalizing`)  
2. HEAD == baseRevision  
3. Scopes OK (if contract)  
4. No original-checkout escape  
5. Sentinel OK  
6. Patch + hash OK  
7. Contract requiredValidation all 0 (if present)  
8. Build gate OK  
9. Shared safety (sandbox/auth) OK  
10. `blockers` empty (including no `temp-index-retained`)  
11. At least one changed path (else blocker `no-changes`)

**`/grok:handoff` observed ready** (what parents must use for integration) is true only when **all** of:

1. Valid handoff manifest loads and passes `validate_implementation_handoff`.  
2. Manifest `integration.ready === true`.  
3. Patch re-hash matches.  
4. A **valid completed terminal envelope** exists for the same `runId` (envelope success / effective lifecycle `completed` per §6).  

If manifest says ready but envelope is missing or not success → handoff reports `integration.ready: false` with blocker `terminal-envelope-incomplete` (or equivalent exact string locked as `terminal-envelope-incomplete`). Status remains read-only; recovery finishes lifecycle via §7.1 when a writer runs.

**Crash tests:**

- Manifest written, envelope not written → handoff not integration-ready.  
- Envelope written, lifecycle not written → handoff may observe ready if envelope success + manifest ready; status derives completed from envelope.  
- Envelope + lifecycle complete → handoff ready true when manifest ready.

### 14.13 Command evidence

Per command: stdout/stderr sha256; redacted tails max **4096** bytes; truncated flags. Optional full logs under `artifacts/commands/` mode 0600. Never full logs on envelope stdout.

Git path listing: use `-z` / NUL-safe parsing only. Tests include paths with spaces, tabs, newlines, and non-ASCII. Never parse porcelain by line splitting alone.

### 14.14 `/grok:handoff`

Files:

```text
plugin/skills/handoff/SKILL.md
plugin/skills/handoff/run.mjs
plugin/wrapper/scripts/groklib/modes/handoff.py
```

Companion:

- Add `handoff` to `WRAPPER_MODES` only.  
- **Do not** add to `STREAMING_MODES`.  
- Dedicated `runHandoff()` equivalent to `runStatus()` passthrough (no job creation, no live relay, no progress adoption).  
- Stderr must not contaminate single JSON stdout (same discipline as status).  

Behavior: read-only; `--run-id` only; rehash patch; same `validate_implementation_handoff`; `artifact-integrity-failure` / `handoff-unavailable`. No apply/commit/merge/push/cleanup.

**Observed integration.ready** for the returned envelope must apply §14.12 dual condition (manifest ready **and** completed terminal envelope). Tests: no Grok process; no companion job; ready false when manifest ready but envelope missing.

### 14.15 Parent protocol (document only)

Same 14 steps as rev 5 (dispatch → wait → handoff → ready → hash → inspect → base still present → dirty overlap check → `git apply --check --binary` → explicit apply → revalidate parent → record runId+hash). No auto-apply in PR4.

### 14.16 Parallel peers

Suitable/unsuitable lists unchanged from rev 5. Disjoint write scopes; dependents re-base after integrate A. One target workspace per run.

### 14.17 Cleanup language (factual only)

When cleaning a run with `integration.ready === true`, warn with **exactly** this meaning (wording may match):

```text
This run contains an integration-ready handoff. Cleanup will permanently remove its retained worktree and stored handoff artifacts. The plugin cannot determine whether the implementation was integrated.
```

Do **not** say “unacknowledged.” No acknowledgment state exists.

### 14.18 Error classes / tests / docs / release

As §12, §14.6–14.14 tests, dual-host smoke §14.19, packaging 1.6.0 on the three version paths.

### 14.19 Dual-host smoke

1. Claude: code → status → handoff  
2. Codex: code → status → handoff  
3. Failed code → forensic handoff ready false  
4. Tampered patch → integrity failure  
5. Explicit cleanup after inspection  
6. Status on failed target: envelope visible despite exit 1  

## 15. Success criteria (full program)

- [ ] PR1–PR3 criteria with CAS + read-only status  
- [ ] Terminal envelope never replaced  
- [ ] Crash-consistent envelope-first terminal persistence + recovery  
- [ ] Code handoff ready from terminalOutcome; handoff dual-condition ready  
- [ ] Contract validation under operator-trust (no false OS-sandbox claim)  
- [ ] `/grok:handoff` non-streaming integrity  
- [ ] Dual-host smoke including failed-target status envelope  

## 16. Out of scope

- Host chat completion APIs  
- Untracked under review `--isolated`  
- Windows native notify  
- Ignore-list review safety  
- Auto-apply handoff  
- `--allow-commits`  
- Status-driven durable interrupted persistence  
- Automatic notification retry  
- Multi-root build gates in one code run  
- Operator notify-retry command (future PR)  

## 17. New source file conventions

Every new Python/JS/Markdown/skill file must follow repository path-header and skill-frontmatter rules (existing `modes/*.py` header style; skill YAML frontmatter; `run.mjs` self-locating pattern).

## 18. Review findings map

### Original 24 (rev 5→6) — complete

| # | Resolution |
|---|------------|
| 1–24 | As rev 6 map; residual gaps closed in rev 7 below |

### Residual findings on rev 6 (closed in rev 7)

| Residual # | Severity | Resolution |
|------------|----------|------------|
| R1 | Critical | §7.1 envelope-first; status §6 envelope-derived lifecycle; idempotent finish |
| R2 | Critical | §14.6–14.12 `terminalOutcome`; handoff dual-condition ready |
| R3 | High | §14.3 / §14.9 operator-trusted, no OS FS sandbox claim |
| R4 | High | §9.4 parent recovery writer + enumerated classes |
| R5 | High | §10 `--ita-invisible-in-index` + `git add -N` test |
| R6 | Medium | Plan PR3 exact file list (no optional paths) |
| R7 | Medium | §14.7 mechanical temp-index post-check + `temp-index-retained` |

### Consistency fixes on rev 7 (closed in rev 8)

| # | Severity | Resolution |
|---|----------|------------|
| C1 | High | §9 / §9.4 require `proc.is_alive() is False`; unkillable → ephemeral only |
| C2 | Medium | §11 skill-run.mjs no functional change (matches plan) |
| C3 | Medium | Plan: rescue always prefixes env; adversarial-review isolation definitive |
| C4 | Low | Plan: seven PR4 envelope error classes including `terminal-envelope-incomplete` |
