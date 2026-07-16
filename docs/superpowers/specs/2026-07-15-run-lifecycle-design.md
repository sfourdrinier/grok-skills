# Run lifecycle, isolated review, and completion signals

**Status:** design (brainstorm approved; plan revision 2 after execution review)  
**Date:** 2026-07-15  
**Product:** grok-skills (Claude Code + Codex)  
**Baseline:** v1.2.10

## 1. Problem

Background and long-running Grok modes (especially `review`) are hard to trust:

1. **Lifecycle truth is fuzzy.** Status can look like success while the target is unfinished or failed.
2. **Run id is advertised early.** `create_run()` emits `[grok-run-id]` before a durable seed `run.json` exists.
3. **Progress is uneven.** Post-Grok finalization is quiet; operators think the process is stuck.
4. **Finalization can hang.** No guaranteed terminal envelope after model exit.
5. **Review evidence can be poisoned** by concurrent writers on the live checkout.
6. **No clean completion signal** for background jobs (optional notifications; never chat injection).

## 2. Goals

- Durable **target lifecycle** for every live run.
- **Atomic seed** `run.json` before the run id is published.
- **Atomic, validated** terminal `envelope.json` before a run is considered finished.
- Status projection that matches the **both, versioned** model (see §6).
- Dense **phase progress** with elapsed time.
- **Finalization watchdog** (process-based, cross-platform) that always leaves a classified terminal envelope.
- **Isolated review** for `--base` and `--isolated`; fail closed if isolation fails.
- Optional **notifications** after terminal envelope; at-most-once; never fail the run.
- Dual-host: same core lifecycle; harnesses only adapt presentation.
- **Docs follow code** (README, CHANGELOG, docs/\*\*, skills, references, roadmap) on every shippable PR.

## 3. Non-goals

- Injecting completions into Claude/Codex chat from the Python subprocess.
- Broad ignore lists as the read-only safety model.
- A second durable stream beside `progress.jsonl`.
- Failing a completed run because notification delivery failed.

## 4. Decisions (locked)

| Topic | Decision |
|-------|----------|
| Lifecycle representation | **Both, versioned:** `run.json` / `response.target.lifecycle` is source of truth. Top-level envelope `status` is a **projection** (table in §6). |
| Seed record | `lifecycle: "created"` and compatible `status: "running"` (not `status: "created"`). |
| Terminal persist | `persist_terminal_envelope(paths, envelope, *, lifecycle)` — **caller passes** terminal lifecycle (`completed` \| `failed` \| `canceled`); never inferred only from envelope `status`. |
| Finalization budget | **Process-based** deadline (not threads). See §9. |
| Review isolation | `--base` ⇒ detached worktree at **HEAD**, keep original `--base` for comparison. `--isolated` ⇒ detached worktree at HEAD + apply **tracked** dirty changes. Fail: `isolation-unavailable`, no live fallback. |
| Notifications | Default **off**. Setup opt-in: off \| auto \| native \| webhook. Recommend **auto**. Canonical storage: **jobs index config** (same family as `runMode`), not gate-state. **At-most-once** via per-run marker. |
| Native notify safety | `spawn`/`spawnSync` with **argv array only**, no shell; bounded timeout; failures log only. |
| Delivery | **Three PRs.** Versions: PR1 **1.3.0**, PR2 **1.4.0**, PR3 **1.5.0**. |

## 5. Architecture

```text
Companion (Node): jobs, live relay, status passthrough, notify after terminal
        │
        ▼
Wrapper (Python): runstate + modes + optional review isolation + process finalize budget
        │
        ▼
~/.local/state/grok-skills/runs/<id>/{run.json, progress.jsonl, envelope.json, owner.json, owner.pid, notified.json}
```

## 6. Target lifecycle and status projection

### Lifecycle (source of truth on `run.json.lifecycle`)

```text
created → running → finalizing → completed
                              ↘ failed
                              ↘ canceled
                              ↘ interrupted   (status-time classification only if process dead + no envelope)
```

| Lifecycle | Meaning |
|-----------|---------|
| `created` | Seed record written; run id may now be published |
| `running` | Active work before post-model finalize |
| `finalizing` | Model/main work finished; packaging / verify / write envelope |
| `completed` | Valid success envelope persisted |
| `failed` | Valid failure envelope persisted (incl. finalization-timeout, isolation-unavailable) |
| `canceled` | Operator cancel with terminal envelope |
| `interrupted` | Status-time only: owner process dead, no valid terminal envelope |

### Top-level `status` projection (both, versioned)

| Target lifecycle | Top-level status | Exit (status mode) |
|------------------|------------------|--------------------|
| `created`, `running`, `finalizing` | `running` | 0 |
| `completed` | `success` | 0 |
| `failed`, `canceled`, `interrupted` | `failure` | 1 |
| Status cannot load/own run; or stored envelope unreadable/invalid C4 | `failure` | 1 |

**Clarification:** Top-level `failure` for `failed` / `canceled` / `interrupted` means “the **target** did not complete successfully,” not “status CLI crashed.” Status mode still returns a well-formed status envelope with `mode: "status"` and `response.target.lifecycle` set. Live modes that *finish* still write their own terminal envelopes with `success`/`failure` as today.

Live-mode terminal envelopes (review/code/…): unchanged (`success` / `failure` on the run’s own envelope). Lifecycle on `run.json` carries `completed` / `failed` / `canceled`.

### Seed record shape

```json
{
  "schemaVersion": 1,
  "runId": "...",
  "mode": "review",
  "createdAtUtc": "...",
  "lifecycle": "created",
  "status": "running",
  "progressStreamPath": "...",
  "envelopePath": "..."
}
```

`status: "running"` remains the legacy-compatible “in flight” marker; `lifecycle` is authoritative for new clients.

## 7. Durable invariants

1. Emit `[grok-run-id]` **only after** atomic seed `run.json` exists (`lifecycle: created`, `status: running`).
2. Treat a run as finished only after atomic, validated `envelope.json`.
3. `persist_terminal_envelope(paths, envelope, *, lifecycle)` requires explicit terminal lifecycle in `{completed, failed, canceled}`.
4. Atomic writes: temp + `os.replace` / rename, mode 0600.
5. Single progress stream: `progress.jsonl` with phase vocabulary + `elapsedMs`.

## 8. Progress phases

`start` → `validate` → `authhome` → `prepare` → `grok` → `finalizing` → (`notify`) → `done`

Status `response.target` includes: `lifecycle`, `process`, `elapsedMs`, `lastProgressAt`, `lastEvent`, `recentEvents`, `eventCount`, `resultAvailable`, `hasStoredEnvelope`.

## 9. Finalization watchdog (process-based)

**Do not use threads to interrupt blocked finalize.**

**Chosen mechanism:**

1. After Grok child exits, parent sets lifecycle `finalizing`, emits progress.
2. Parent runs the **finalize package** (sandbox verify, drift, envelope build, atomic persist) in a **child process** (`multiprocessing` spawn, or a dedicated small entry invoked via `subprocess` with the same Python):
   - Child receives run id / paths / needed context via temp JSON (0600) or argv + env.
   - Child writes progress events to the same `progress.jsonl` (append-only, careful locking or parent-only progress if simpler).
3. Parent waits with **`Process.join(timeout=budget)`** or **`subprocess.run(..., timeout=budget)`**.
4. On timeout: parent **terminates** the child process tree, writes failure envelope `finalization-timeout` via `persist_terminal_envelope(..., lifecycle="failed")`, exits non-zero.
5. On success: parent confirms envelope on disk + lifecycle terminal.

**Budgets:** review/reason **120s**, code/verify **180s**; `GROK_FINALIZE_TIMEOUT_SECONDS` clamp 30–600.

**Cooperative logging** inside the child (step markers) remains required so status can show `finalizing` with recent events.

## 10. Isolated review

### When required

| Invocation | Isolation |
|------------|-----------|
| `--base <ref>` | **Required:** detached worktree at **HEAD**; keep operator’s `--base` for comparison semantics |
| `--isolated` (no base) | **Required:** detached worktree at **HEAD**, then apply **tracked** dirty changes from the original checkout |
| neither | Live checkout; FS drift remains informational warnings only |

### `--base` algorithm (locked)

1. Resolve repo root from target.  
2. Create detached worktree at **HEAD** under state-owned path (e.g. worktrees/review/`runId`).  
3. Run review with cwd/workspace = worktree; **pass through original `--base`** so base-diff / prompt logic is unchanged.  
4. On failure to create: `isolation-unavailable`, no live fallback.  
5. Best-effort remove worktree on terminal.

### `--isolated` without base (locked, no “pick one”)

1. Create detached worktree at **HEAD** (same layout as above).  
2. From original checkout, capture **tracked** dirty state only:  
   `git diff HEAD` (and include staged+unstaged tracked via `git diff HEAD` which covers both when used as `git diff HEAD` + ensure index; use `git write-tree`/`git diff HEAD` pipeline).  
   Concrete implementation:  
   - `git -C source diff HEAD --binary > /tmp/diff`  
   - `git -C worktree apply --index` (or `git apply` then refresh)  
3. **Untracked files are not applied** in v1 of `--isolated` (document; avoids copying secrets/build artifacts by surprise). Operators who need untracked in scope use commit/stash first or a later enhancement.  
4. On apply failure: `isolation-unavailable`.  
5. Cleanup as above.

## 11. Notifications

### Canonical storage

**Jobs index config** (same file family as `runMode` in `plugin/scripts/lib/jobs.mjs` index):

```json
"config": {
  "runMode": "hardened",
  "notificationMode": "off",
  "notificationWebhookUrl": null
}
```

Gate state (`gate-state.json`) remains **gate-only**. Do not mix notification prefs into the gate file.

### Modes

| Mode | Behavior |
|------|----------|
| `off` | Never (default) |
| `auto` | Background jobs only; after terminal envelope; if native channel exists |
| `native` | Attempt desktop notification when available |
| `webhook` | POST minimal JSON; short timeout; never fail run |

### At-most-once

After a successful notify attempt (or deliberate skip for `off`), create **`runs/<id>/notified.json`** with `os.open(..., O_CREAT|O_EXCL)` (or write-temp+rename of a “claimed” marker before send).  

- If marker exists → skip (no second notify).  
- Crash **after** send but **before** marker → possible **one duplicate**; document as residual. Prefer: write marker with `status: "pending"` under exclusive create, then send, then update marker to `sent` — if pending and process dies, restart may retry once (document as at-most-twice under crash). **Policy locked:** exclusive create of `notified.json` with `{ "state": "pending" }` then send then update to `sent`. If state is `sent`, never notify. If `pending` and age &lt; 5m, skip retry (assume in-flight). If `pending` and age ≥ 5m, allow one retry.

### Native adapter rules

- **argv array only** (`spawnSync(cmd, args, { shell: false, timeout })`).  
- No string shell, no interpolation of run id into shell scripts.  
- Timeout e.g. 5s.  
- Any failure → stderr log only; run already terminal.

### Payload (default)

run id, mode, lifecycle/result, duration seconds — no prompt, model text, paths, secrets.

## 12. Error classes

| Class | When |
|-------|------|
| `isolation-unavailable` | Required isolation could not be created or dirty apply failed |
| `finalization-timeout` | Finalize child exceeded budget |

## 13. Three PRs

| PR | Version | Scope |
|----|---------|--------|
| **PR1** | **1.3.0** | Lifecycle core, seed-before-id, atomic envelope, status projection table, progress, process finalize watchdog |
| **PR2** | **1.4.0** | Isolated review (`--base` / `--isolated`) |
| **PR3** | **1.5.0** | Notifications + setup surface + dual-host docs |

## 14. Success criteria

- [ ] No run id without seed `run.json` (`lifecycle: created`, `status: running`)  
- [ ] Status projection matches §6 table  
- [ ] `persist_terminal_envelope` always takes explicit terminal lifecycle  
- [ ] Finalize hang → `finalization-timeout` envelope  
- [ ] `--base` never silent-falls-back to live checkout  
- [ ] Notifications default off; at-most-once marker; safe spawn  
- [ ] Docs lists complete per AGENTS.md rule #1  

## 15. Out of scope (listed)

- Host chat completion callbacks (until hosts provide APIs)  
- Applying untracked files under `--isolated` v1  
- Ignore-list-based review “safety”
