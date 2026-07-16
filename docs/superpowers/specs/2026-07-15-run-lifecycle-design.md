# Run lifecycle, isolated review, and completion signals

**Status:** design (brainstorm approved)  
**Date:** 2026-07-15  
**Product:** grok-skills (Claude Code + Codex)  
**Baseline:** v1.2.10 (status reports `running` for live owner + no envelope; no post-status stderr dump)

## 1. Problem

Background and long-running Grok modes (especially `review`) are hard to trust:

1. **Lifecycle truth is fuzzy.** Status can look like success while the target is unfinished; missing `envelope.json` was treated as a warning instead of an in-progress state (partially fixed in 1.2.10). Dead owner + no envelope is still a weak story.
2. **Run id is advertised early.** `create_run()` emits `[grok-run-id]` before the mode writes the initial `run.json`, so there is a window where status cannot fully explain the run.
3. **Progress is uneven.** Model work is visible; post-Grok finalization is quiet, so operators think the process is stuck after Grok finishes.
4. **Finalization can hang.** If the wrapper dies after the model but before a terminal envelope, status has no durable result.
5. **Review evidence can be poisoned** by concurrent writers (`.next`, `.pid`, logs) on the live checkout. Drift is warning-only today; attribution is still weak.
6. **No clean completion signal** for background jobs (notifications optional, never chat injection from the wrapper).

## 2. Goals

- Durable, observable **target lifecycle** for every live run.
- **Atomic seed** `run.json` before the run id is published.
- **Atomic, validated** terminal `envelope.json` before a run is considered finished.
- Status that never confuses **transport** (lookup worked) with **target result**.
- Dense **phase progress** (including finalizing) with elapsed time.
- **Finalization watchdog** that always leaves a classified terminal envelope.
- **Isolated review** when `--base` is set, or when `--isolated` is set; fail closed if isolation cannot be created.
- Optional **notifications** (off by default; setup can opt into auto/native/webhook).
- Dual-host: same core lifecycle; harnesses only adapt presentation.

## 3. Non-goals

- Injecting completion messages into Claude Code or Codex chat from the Python process.
- Broad filesystem ignore lists as the primary read-only guarantee.
- A second durable stream beside `progress.jsonl` (lifecycle events go in the same stream with a clear phase vocabulary).
- Making notifications required for a green run (delivery failure must never fail the run).

## 4. Decisions (locked in brainstorm)

| Topic | Decision |
|-------|----------|
| Lifecycle representation | **Both, versioned:** `response.target.lifecycle` is the source of truth. Top-level envelope `status` is a **projection** for older clients (`running` / `success` / `failure`). |
| Review isolation | Isolated **when `--base` is set**. Explicit **`--isolated`** for working-tree reviews. If isolation cannot be created → **`isolation-unavailable`**, never silent live-checkout fallback. |
| Notifications | Default **`off`**. Setup offers opt-in: `off` \| `auto` \| `native` \| `webhook`. Recommend **`auto`** during setup. `auto` = background only, after terminal envelope, if a desktop channel exists. |
| Delivery shape | **Three PRs** covering full scope (fewest dense deliverables). |

## 5. Architecture

```text
                    ┌─────────────────────────────────────┐
                    │  Companion (Node)                    │
                    │  - job registry, live relay stderr   │
                    │  - status: one stdout envelope only  │
                    │  - notifications after terminal env  │
                    └──────────────┬──────────────────────┘
                                   │ spawn
                    ┌──────────────▼──────────────────────┐
                    │  Wrapper (Python)                    │
                    │  runstate: dir, run.json, progress,  │
                    │            envelope, owner.pid       │
                    │  modes: create → run → finalize      │
                    │  review: optional isolated worktree  │
                    └─────────────────────────────────────┘
```

**Units:**

| Unit | Responsibility |
|------|----------------|
| `runstate` | Atomic run dir seed, lifecycle field on `run.json`, run-id emit order, envelope write helper |
| `progress` | Phase vocabulary + `elapsedMs` on events |
| Mode runners (`_shared`, `_worktree`, review) | Transition lifecycle; finalization watchdog |
| `status` | Map disk + process → `target.lifecycle` + top-level projection |
| Review isolation | Temp worktree / snapshot for review when required |
| Notifications | Platform adapters; companion or thin post-hook after envelope exists |
| Host docs/skills | Document flags, status shapes, setup notification prefs |

## 6. Target lifecycle (source of truth)

Stored on **`run.json`** as `lifecycle` (and mirrored in status `response.target.lifecycle`).

```text
created → running → finalizing → completed
                              ↘ failed
                              ↘ canceled
                              ↘ interrupted
```

| Lifecycle | Meaning |
|-----------|---------|
| `created` | Seed record written; work not started or just starting |
| `running` | Model/work in progress (Grok child or mode body before finalize) |
| `finalizing` | Grok (or main work) finished; wrapper validating / packaging result |
| `completed` | Valid terminal success envelope persisted |
| `failed` | Valid terminal failure envelope persisted (including finalization-timeout, isolation-unavailable, etc.) |
| `canceled` | Operator cancel recorded with terminal envelope |
| `interrupted` | Owner process dead, no valid terminal envelope |

### Status command mapping

| Target condition | `target.lifecycle` | Top-level `status` (projection) | Exit |
|------------------|--------------------|----------------------------------|------|
| Live owner, no envelope, not yet finalizing | `running` | `running` | 0 |
| Live owner, no envelope, post-Grok finalize | `finalizing` | `running` | 0 |
| Envelope exists + validates, success | `completed` | `success` | 0 |
| Envelope exists + validates, failure | `failed` | `success`* | 0 |
| Owner dead, no envelope | `interrupted` | `success`* | 0 |
| Envelope unreadable / invalid C4 | treat as `failed` (status query returns failure class `output-malformed`) | `failure` | 1 |
| Cancel recorded | `canceled` | `success`* | 0 |

\*Top-level `success` means **status lookup / inspection succeeded**, not “the review passed.” The review outcome lives in `response.storedEnvelope.status` and/or `target.lifecycle` / `target.resultAvailable`.

Document this clearly in the status skill and authority-policies so hosts do not misread it.

### Top-level envelope `status` values (projection)

Keep C4-ish set: `success` | `failure` | `running`.

- Live modes that finish write terminal envelopes with `success` or `failure` as today.
- Status mode uses `running` only while the **target** is still in flight.
- Do **not** put `completed` / `interrupted` on top-level status; those live only on `target.lifecycle`.

## 7. Durable invariants

1. **Do not emit `[grok-run-id]` until** the run directory exists with owner marker, liveness marker, and an **atomically written** initial `run.json` (`lifecycle: created` or `running`).
2. **Do not treat a run as finished until** `envelope.json` has been written via temp+rename and validated as a C4 document.
3. **Every terminal path** (success, classified failure, cancel, finalization timeout, isolation failure) updates `run.json.lifecycle` and leaves either a valid envelope or an explicit `interrupted` state discoverable by status after process death.
4. **Atomic writes:** `run.json` and `envelope.json` use write-temp-then-rename (same pattern as Codex agent TOML).
5. **Progress is one stream:** `progress.jsonl` only; events include `phase` from a fixed vocabulary and `elapsedMs` from run start.

## 8. Progress phase vocabulary

Canonical phases (extend existing, do not invent a second log):

| Phase | When |
|-------|------|
| `start` | Run created / seed record |
| `validate` | Binary/auth/preflight-cache |
| `authhome` | Private home lifecycle |
| `prepare` | Isolation / worktree / prompt assembly |
| `grok` | Model execution / streaming |
| `finalizing` | After Grok exit: sandbox verify, drift report, envelope build |
| `notify` | Optional notification attempt (never blocks terminal write) |
| `done` | Terminal record written |

Each event should carry at least: `seq`, `ts`, `phase`, `message`, `level`, and when cheap: `elapsedMs`, `data` object.

Status `response.target` should expose at least:

```json
{
  "lifecycle": "finalizing",
  "recordStatus": "running",
  "process": "alive",
  "elapsedMs": 181492,
  "lastProgressAt": "2026-07-16T02:15:11Z",
  "lastEvent": { "phase": "finalizing", "message": "..." },
  "recentEvents": [],
  "eventCount": 42,
  "resultAvailable": false,
  "hasStoredEnvelope": false
}
```

## 9. Finalization watchdog

After the Grok child process exits (or mode body reaches “build envelope”):

1. Transition lifecycle → `finalizing`; emit progress event.
2. Start a wall-clock budget (default **120s** for review/reason; **180s** for code/verify; env override `GROK_FINALIZE_TIMEOUT_SECONDS` capped reasonably).
3. On success path: validate envelope, atomic write, lifecycle → `completed` or `failed` matching envelope, emit `done`.
4. On budget exceeded: write failure envelope with error class **`finalization-timeout`**, lifecycle → `failed`, kill leftover work if needed, exit non-zero.

Watchdog must not leave a live process with no terminal envelope beyond the budget.

## 10. Isolated review

### When isolation is required

| Invocation | Isolation |
|------------|-----------|
| `review` / `adversarial-review` with `--base <ref>` | **Required** temp worktree at resolved base (or merge-base/HEAD policy documented in plan) |
| `review` with `--isolated` (no base) | **Required** snapshot path for dirty tree (tracked changes; define untracked policy in PR2) |
| `review` neither | Live checkout; FS drift remains **informational warnings** only (current 1.2.2 behavior) |

### Failure

If isolation setup fails: error class **`isolation-unavailable`**, message actionable, **do not** fall back to live checkout.

### Attribution

Inside an isolated tree, unexpected writes are attributable to the run. Outside isolation, do not hard-fail review on concurrent host noise (keep warnings).

## 11. Notifications

Config (workspace or plugin data, via `setup`):

```text
off | auto | native | webhook
```

Default: **off**.

| Mode | Behavior |
|------|----------|
| `off` | Never |
| `auto` | Background jobs only; after terminal envelope; if desktop channel available |
| `native` | Force desktop attempt when available |
| `webhook` | POST minimal JSON to configured URL; short timeout; never fail the run |

**Payload (default):** run id, lifecycle/result, duration, mode — **no** prompt, model body, paths, or secrets.

**Platforms:** macOS `osascript` Notification Center; Linux `notify-send` if present; Windows PowerShell toast if present; else no-op for native/auto.

**Timing:** only after terminal envelope is on disk and validated.

## 12. Host adapters

| Host | Responsibility |
|------|----------------|
| Companion | Jobs table, live relay during foreground, status passthrough (no stderr progress dump), trigger notify after background completion |
| Claude / Codex | Document status shapes; optional future completion hooks if host exposes them — not required for this program |
| Wrapper | Owns lifecycle, isolation, envelope, watchdog |

## 13. Error classes (new or clarified)

| Class | When |
|-------|------|
| `isolation-unavailable` | Required isolation could not be created |
| `finalization-timeout` | Post-Grok finalize exceeded budget without terminal envelope |

Both produce valid failure envelopes and `lifecycle: failed`.

## 14. Deliverables (three PRs)

### PR1 — Run lifecycle core (reliability)

- Atomic seed `run.json` before run-id marker  
- Lifecycle field + transitions  
- Atomic envelope write helper  
- Status: full lifecycle table; transport vs target documented  
- Progress phases + `elapsedMs`  
- Finalization watchdog + `finalization-timeout`  
- Tests: create ordering, status matrix, watchdog fake clock / fake hang  
- Docs: status skill, authority-policies, CHANGELOG  

**Does not include:** review isolation, notifications.

### PR2 — Isolated review

- Worktree isolation when `--base` set  
- `--isolated` for working-tree  
- `isolation-unavailable` fail-closed  
- Tests: concurrent write outside worktree cannot appear as run writes; base-ref isolation  
- Docs: review / adversarial-review skills, README flags  

### PR3 — Notifications + dual-host surface

- Notification preference storage + setup flags  
- Platform adapters + webhook  
- Wire after terminal envelope (companion background path + wrapper hook if cleaner)  
- Docs: setup skill, SECURITY/README short note  
- Dual-host smoke checklist update  

## 15. Testing strategy

- **Unit:** lifecycle transitions, atomic write helpers, status matrix (alive/dead/envelope/malformed), watchdog timeout classification, isolation flag matrix.  
- **Node:** companion status stdout purity; optional notify does not corrupt stdout.  
- **Integration / manual:** long review with status polls showing `running` → `finalizing` → `completed`; kill -9 mid-run → `interrupted`; failed worktree → `isolation-unavailable`.  
- **Regression:** 1.2.2 drift warnings still informational when not isolated; secret redaction on status events unchanged.

## 16. Risks

| Risk | Mitigation |
|------|------------|
| Envelope schema growth breaks strict clients | Top-level status projection stays small; new fields under `response.target` |
| Isolation slows review | Only when `--base` or `--isolated`; document cost |
| Watchdog false timeout | Generous defaults; env override; emit progress during finalize so budgets are diagnosable |
| Notification spam | Default off; auto only background |

## 17. Success criteria (program complete)

- [ ] No run id published without seed `run.json`  
- [ ] Status never reports top-level success for a live unfinished target without `target.lifecycle` making state obvious  
- [ ] Dead owner + no envelope → `interrupted`  
- [ ] Every normal terminal path leaves a validated envelope  
- [ ] Finalization hang becomes `finalization-timeout` envelope  
- [ ] `--base` review never silently uses live checkout  
- [ ] Notifications optional, after envelope only, never fail the run  
- [ ] Claude and Codex documented against the same lifecycle  

## 18. Out of scope for later (listed, not forgotten)

- Host-native “completion callback into chat” when/if APIs exist  
- Full dirty-tree snapshot of arbitrary untracked trees without `--isolated`  
- Ignore-list-based “safety” for review  
- Merging lifecycle into a second file format  

---

**Next step after approval:** implementation plan at `docs/superpowers/plans/2026-07-15-run-lifecycle.md` with task checklists per PR.
