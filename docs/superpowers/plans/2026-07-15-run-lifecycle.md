# Run lifecycle program — Implementation Plan (revision 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship durable run lifecycle, honest status projection, process-based finalization watchdog, isolated review, and optional at-most-once notifications — full Codex feedback letter in three dense PRs.

**Architecture:** `run.json` + `progress.jsonl` + `envelope.json` are durable. `lifecycle` on the record is source of truth. Top-level envelope `status` is a projection per design §6. Finalize runs in a **child process** with join timeout. Review isolation uses detached worktree at HEAD. Notifications live in **jobs index config** and fire only after terminal envelope + exclusive notify marker.

**Tech stack:** Python 3 stdlib wrapper, Node companion, unittest + `node --test`.

**Design spec:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md) (revision 2)

**Baseline:** v1.2.10 on `main`.

**Versions:** PR1 → **1.3.0**, PR2 → **1.4.0**, PR3 → **1.5.0**.

---

## Projection table (do not regress)

| Target lifecycle | Top-level status | Status-mode exit |
|------------------|------------------|------------------|
| `created`, `running`, `finalizing` | `running` | 0 |
| `completed` | `success` | 0 |
| `failed`, `canceled`, `interrupted` | `failure` | 1 |
| Load/own/malformed envelope errors | `failure` | 1 |

---

## File map

### PR1

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/groklib/runstate.py` | Atomic JSON; seed `run.json`; emit run-id after seed; `set_lifecycle`; `persist_terminal_envelope(..., lifecycle=)` |
| `plugin/wrapper/scripts/groklib/progress.py` | `elapsedMs` support |
| `plugin/wrapper/scripts/groklib/modes/_shared.py` | Transitions; process-based finalize |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | Same for code/verify |
| `plugin/wrapper/scripts/groklib/modes/status.py` | Projection table + target fields |
| `plugin/wrapper/scripts/groklib/envelope.py` | `finalization-timeout` error class; STATUSES stay success/failure/running |
| `plugin/wrapper/scripts/tests/test_runstate.py` | New or extended |
| `plugin/wrapper/scripts/tests/test_mode_status.py` | Full matrix |
| `plugin/wrapper/scripts/tests/test_envelope.py` | exit_code_for + new class |
| `plugin/wrapper/scripts/tests/test_mode_*.py` | Wire finalize if needed |
| **Docs PR1:** `plugin/skills/status/SKILL.md`, `plugin/wrapper/references/authority-policies.md`, `plugin/wrapper/SKILL.md` (status/lifecycle note if present), `README.md` (status / troubleshooting), `docs/COMPATIBILITY.md` (incomplete runs / status), `docs/roadmap.md` (lifecycle shipped), `CHANGELOG.md`, packaging versions, `docs/RELEASE.md` if ship notes |

### PR2

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--isolated` |
| `plugin/wrapper/scripts/groklib/review_isolation.py` | **New** — worktree at HEAD; dirty apply |
| `plugin/wrapper/scripts/groklib/modes/review.py` | Require isolation; fail closed |
| `plugin/wrapper/scripts/groklib/envelope.py` | `isolation-unavailable` |
| `plugin/wrapper/scripts/tests/test_mode_review.py` | Isolation tests |
| `plugin/wrapper/scripts/tests/test_review_isolation.py` | Unit isolation helper |
| **Docs PR2:** `plugin/skills/review/SKILL.md`, `plugin/skills/adversarial-review/SKILL.md`, `README.md` flags, `plugin/references/README.md` if skill table, `plugin/wrapper/references/authority-policies.md`, `docs/COMPATIBILITY.md`, `docs/roadmap.md`, `CHANGELOG.md`, packaging versions |

### PR3

| Path | Role |
|------|------|
| `plugin/scripts/lib/jobs.mjs` | **Canonical** `config.notificationMode` + `notificationWebhookUrl` |
| `plugin/scripts/lib/notify.mjs` | **New** — off/auto/native/webhook; safe spawn; at-most-once marker under run dir |
| `plugin/scripts/grok-companion.mjs` | setup flags; background notify after terminal |
| `plugin/scripts/tests/notify.test.mjs` | **New** |
| `plugin/scripts/tests/jobs.test.mjs` | Config persistence |
| **Docs PR3:** `plugin/skills/setup/SKILL.md`, `README.md`, `docs/RELEASE.md`, `plugin/references/manual-smoke.md`, `docs/COMPATIBILITY.md`, `docs/roadmap.md`, `CHANGELOG.md`, packaging **1.5.0**, `SECURITY.md` one-line if notify webhook mentioned |

---

## PR1 — Run lifecycle core (→ 1.3.0)

### Task 1.1 — Atomic write + seed before run-id

**Files:** `runstate.py`, `tests/test_runstate.py`

- [ ] **Step 1: Failing tests**

```python
def test_create_run_seed_shape_and_order(self):
    paths = runstate.create_run("review")
    record = json.loads((paths.run_dir / "run.json").read_text(encoding="utf-8"))
    self.assertEqual(record["lifecycle"], "created")
    self.assertEqual(record["status"], "running")  # NOT "created"
    self.assertEqual(record["runId"], paths.run_id)
    self.assertTrue((paths.run_dir / "owner.json").is_file())

def test_write_json_atomic_replaces(self):
    # write twice; final content is last payload; no .tmp left
    ...
```

- [ ] **Step 2: Run — expect FAIL**

```bash
cd plugin/wrapper/scripts && python3 -m unittest tests.test_runstate -q
```

- [ ] **Step 3: Implement**

```python
def write_json_atomic(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp." + str(os.getpid()))
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    # write 0600, os.replace(tmp, path)

def create_run(mode: str) -> RunPaths:
    # mkdir, owner, liveness, trace
    # write_json_atomic(run.json) with lifecycle="created", status="running", mode=mode, ...
    # THEN emit_run_id_marker(run_id)
    # return paths
```

- [ ] **Step 4: Tests PASS; commit**

```bash
git commit -m "runstate: seed run.json (lifecycle created) before run-id marker"
```

### Task 1.2 — Lifecycle transitions

**Files:** `runstate.py`, `_shared.py`, `_worktree.py`, tests

- [ ] **Step 1: Tests** for `set_lifecycle` allowed graph and refuse mutation after `completed`/`failed`/`canceled` (interrupted is status-time only on disk unless you persist it — **persist `interrupted` only when status observes dead owner and writes it, or leave classification ephemeral in status response**; prefer: status computes interrupted without rewriting run.json unless optional best-effort write).

**Locked:** Status may set `lifecycle: interrupted` on the record with a best-effort atomic write when it detects dead owner + no envelope (so later polls are stable). If write fails, still return projection `failure` / lifecycle interrupted in response.

- [ ] **Step 2: Implement `set_lifecycle(paths, lifecycle: str) -> dict`**

- [ ] **Step 3: Wire** start → `running`; post-Grok → `finalizing`; terminal via Task 1.3 only.

- [ ] **Step 4: Commit** `runstate: lifecycle transitions`

### Task 1.3 — `persist_terminal_envelope(paths, envelope, *, lifecycle)`

**Files:** `runstate.py` or `envelope.py` + mode emit sites

**Signature (locked):**

```python
def persist_terminal_envelope(
    paths: RunPaths,
    envelope: dict,
    *,
    lifecycle: str,  # must be "completed" | "failed" | "canceled"
) -> None:
    if lifecycle not in ("completed", "failed", "canceled"):
        raise ValueError(...)
    envelope_mod.validate_envelope(envelope)  # or raise
    write_json_atomic(paths.envelope_path, envelope)
    set_lifecycle(paths, lifecycle)
```

- [ ] **Must not** map envelope `status=="failure"` alone to lifecycle; caller passes lifecycle.  
  Examples: cancel path → `lifecycle="canceled"` even if envelope status is failure; normal error → `failed`; success → `completed`.

- [ ] **Tests:** cancel vs failed vs completed call sites / unit with explicit lifecycle.  
- [ ] **Commit:** `envelope: persist_terminal_envelope requires explicit lifecycle`

### Task 1.4 — Progress elapsedMs + finalizing events

**Files:** `progress.py`, finalize path in `_shared.py`

- [ ] ProgressWriter accepts `started_at_monotonic` or reads `createdAtUtc` to stamp `elapsedMs`.  
- [ ] Emit finalizing steps: enter finalize, sandbox verify, drift, build envelope, persist.  
- [ ] **Commit:** `progress: elapsedMs and finalizing phases`

### Task 1.5 — Process-based finalization watchdog

**Files:** `_shared.py` (and `_worktree.py`), `envelope.ERROR_CLASSES`, tests

**Mechanism (locked — no threads for interrupt):**

1. After Grok returns, `set_lifecycle(..., "finalizing")`, progress event.  
2. Run finalize in **child process**:

```python
# parent
proc = multiprocessing.get_context("spawn").Process(
    target=_finalize_worker, args=(worker_payload_path,)
)
proc.start()
proc.join(timeout=budget_seconds)
if proc.is_alive():
    proc.terminate()
    proc.join(5)
    if proc.is_alive():
        proc.kill()
    # persist_terminal_envelope(..., failure finalization-timeout, lifecycle="failed")
    return failure_envelope
# else read result from worker output path; persist already done by worker OR parent promotes
```

Prefer worker writes the terminal envelope itself via `persist_terminal_envelope` so parent only checks exit code + file existence.

3. Budgets: review/reason 120s, code/verify 180s; env clamp 30–600.  
4. **Test:** worker that sleeps past budget → parent produces `finalization-timeout`, lifecycle failed, exit 1.  
5. **Commit:** `modes: process-based finalization watchdog`

### Task 1.6 — Status projection matrix (CORRECTED)

**Files:** `modes/status.py`, `tests/test_mode_status.py`

| Condition | target.lifecycle | top-level status | exit |
|-----------|------------------|------------------|------|
| process alive, no envelope, lifecycle running/created | from record | `running` | 0 |
| process alive, finalizing | `finalizing` | `running` | 0 |
| envelope valid, success | `completed` | `success` | 0 |
| envelope valid, failure | `failed` | **`failure`** | **1** |
| process dead, no envelope | `interrupted` | **`failure`** | **1** |
| cancel recorded | `canceled` | **`failure`** | **1** |
| envelope malformed | — | `failure` + output-malformed | 1 |

- [ ] Update tests that previously expected top-level `success` for dead+no-envelope.  
- [ ] `exit_code_for`: `running` and `success` → 0; `failure` → 1.  
- [ ] No “stored envelope not found” while lifecycle is non-terminal in-flight.  
- [ ] **Commit:** `status: projection table for both-versioned lifecycle`

### Task 1.7 — Docs + 1.3.0 ship

**Docs required (complete list):**

- [ ] `plugin/skills/status/SKILL.md`  
- [ ] `plugin/wrapper/references/authority-policies.md`  
- [ ] `plugin/wrapper/SKILL.md` (if status/lifecycle mentioned)  
- [ ] `README.md` (status / troubleshooting)  
- [ ] `docs/COMPATIBILITY.md`  
- [ ] `docs/roadmap.md`  
- [ ] `CHANGELOG.md`  
- [ ] `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`, `.claude-plugin/marketplace.json` → **1.3.0**  
- [ ] `docs/RELEASE.md` if release checklist needs lifecycle note  

- [ ] Full Python + Node suites green.  
- [ ] Commit + tag `v1.3.0`.

---

## PR2 — Isolated review (→ 1.4.0)

### Task 2.1 — `--isolated` flag

**Files:** `grok_agent.py`, entrypoint tests

- [ ] `store_true` `--isolated`.  
- [ ] Thread into review ModeRun.  
- [ ] **Commit:** `cli: --isolated for review`

### Task 2.2 — `review_isolation.py` (no open design)

**Create:** `plugin/wrapper/scripts/groklib/review_isolation.py`

**API:**

```python
@dataclass
class ReviewIsolation:
    worktree_path: pathlib.Path
    cleanup: Callable[[], None]

def prepare_review_isolation(
    *,
    repo_root: pathlib.Path,
    run_id: str,
    base: str | None,
    isolated: bool,
) -> ReviewIsolation:
    """Raise GrokWrapperError isolation-unavailable on any failure. Never falls back to live checkout."""
```

**`--base` path (locked):**

1. `git worktree add --detach <state>/worktrees/review/<run_id> HEAD`  
2. Return worktree path; caller keeps original `base` for comparison.  

**`--isolated` without base (locked):**

1. Same worktree at HEAD.  
2. `git -C repo_root diff HEAD --binary` → temp file 0600.  
3. If diff non-empty: `git -C worktree apply --whitespace=nowarn` (or `git apply` + handle exit). Empty diff OK.  
4. **Untracked files are not copied** (document in skill).  
5. Apply failure → `isolation-unavailable`.

**Neither flag:** return None isolation; review uses live checkout.

- [ ] **Commit:** `review: isolation helper worktree at HEAD`

### Task 2.3 — Wire review mode

- [ ] If `base` or `isolated`: call prepare; on error emit failure envelope lifecycle failed.  
- [ ] Run review against worktree paths.  
- [ ] Always cleanup isolation in finally.  
- [ ] **Commit:** `review: require isolation for --base and --isolated`

### Task 2.4 — Tests

- [ ] `--base` create mocked to fail → `isolation-unavailable`, no live run.  
- [ ] Isolated tree: mutate original checkout file during review; must not be attributed as worktree escape for the run.  
- [ ] Dirty tracked file appears in isolated tree after apply.  
- [ ] Untracked file in source does **not** appear in worktree.  

### Task 2.5 — Docs + 1.4.0

**Docs required:**

- [ ] `plugin/skills/review/SKILL.md`  
- [ ] `plugin/skills/adversarial-review/SKILL.md`  
- [ ] `README.md`  
- [ ] `plugin/references/README.md`  
- [ ] `plugin/wrapper/references/authority-policies.md`  
- [ ] `docs/COMPATIBILITY.md`  
- [ ] `docs/roadmap.md`  
- [ ] `CHANGELOG.md` + packaging **1.4.0**  

- [ ] Full suites; tag `v1.4.0`.

---

## PR3 — Notifications (→ 1.5.0)

### Task 3.1 — Canonical storage in jobs index

**Files:** `plugin/scripts/lib/jobs.mjs`, `jobs.test.mjs`

- [ ] Extend `config`:

```javascript
config: {
  runMode: "hardened" | "direct",
  notificationMode: "off" | "auto" | "native" | "webhook",  // default "off"
  notificationWebhookUrl: null | string,
}
```

- [ ] `getNotificationPrefs(cwd, env)` / `setNotificationPrefs(...)`.  
- [ ] **Do not** put these fields in `gate-state.json`.  
- [ ] **Commit:** `jobs: notification prefs in index config`

### Task 3.2 — `notify.mjs` with safe spawn + at-most-once

**Create:** `plugin/scripts/lib/notify.mjs`

```javascript
/**
 * @returns {Promise<{ attempted: boolean, sent: boolean, reason?: string }>}
 * Never throws; never writes to stdout.
 */
export async function notifyRunComplete({
  runsDir, // or runDir absolute
  runId,
  mode,
  lifecycle,
  durationSeconds,
  preference, // off|auto|native|webhook
  webhookUrl,
  isBackground,
}) 
```

**At-most-once (locked):**

1. `notifiedPath = path.join(runDir, "notified.json")`  
2. Try exclusive create `{ state: "pending", at: iso }` (wx / O_EXCL). If exists:  
   - if `state==="sent"` → return skipped  
   - if `state==="pending"` and age &lt; 5 minutes → return skipped  
   - if `pending` and age ≥ 5 minutes → allow retry  
3. Send notification.  
4. Rewrite marker `{ state: "sent", at: iso }`.  
5. On send failure: delete pending or leave pending for retry policy; **still never fail the job**.

**Native:**

- macOS: `spawnSync("osascript", ["-e", script], { shell: false, timeout: 5000 })` — build script with **escaped** string literals only, or prefer a fixed script file + env vars without shell.  
- Linux: `spawnSync("notify-send", [title, body], { shell: false, timeout: 5000 })`  
- Windows: `spawnSync("powershell.exe", ["-NoProfile", "-Command", ...], { shell: false, timeout: 5000 })` with careful quoting, or skip if unsafe.  
- Prefer: no user-controlled content in shell metacharacters; run id is strict charset already.

**auto:** only if `isBackground && preference==="auto"` and native available.  
**webhook:** `fetch`/`http` POST JSON, 3s timeout, no secrets.

- [ ] Tests: off; exclusive marker; second call no-op; spawn not called with shell true.  
- [ ] **Commit:** `notify: at-most-once safe desktop and webhook`

### Task 3.3 — Wire companion

- [ ] setup: `--notification-mode`, `--notification-webhook`  
- [ ] After **background** job terminal (wrapper close + envelope or terminal lifecycle), call notify.  
- [ ] Foreground: no auto notify.  
- [ ] Status command: never notify.  
- [ ] **Commit:** `companion: notification setup and background hook`

### Task 3.4 — Docs + 1.5.0

**Docs required:**

- [ ] `plugin/skills/setup/SKILL.md`  
- [ ] `README.md`  
- [ ] `docs/RELEASE.md`  
- [ ] `plugin/references/manual-smoke.md`  
- [ ] `docs/COMPATIBILITY.md`  
- [ ] `docs/roadmap.md`  
- [ ] `SECURITY.md` (webhook is opt-in, no payload secrets)  
- [ ] `CHANGELOG.md` + packaging **1.5.0**  

- [ ] Full suites; tag `v1.5.0`.

---

## Cross-PR feedback coverage

| Item | PR |
|------|-----|
| Lifecycle machine | PR1 |
| Seed before run-id; `lifecycle: created` + `status: running` | PR1 |
| Explicit lifecycle on persist_terminal_envelope | PR1 |
| Status projection table (failure for failed/canceled/interrupted) | PR1 |
| Process-based finalize watchdog | PR1 |
| Progress phases + elapsed | PR1 |
| Isolation `--base` worktree at HEAD; keep `--base` | PR2 |
| Isolation `--isolated` tracked dirty apply | PR2 |
| isolation-unavailable no fallback | PR2 |
| Notify storage = jobs config | PR3 |
| At-most-once notified.json | PR3 |
| Safe argv spawn, timeout, never fail run | PR3 |
| Full docs follow code | each PR docs list |
| Semver 1.3 / 1.4 / 1.5 | each ship task |

---

## Execution order

1. Finish **PR1** completely (including tag 1.3.0) before PR2.  
2. **PR2** then tag 1.4.0.  
3. **PR3** then tag 1.5.0.  
4. Do not reintroduce post-status stderr progress dumps.  
5. Keep product skill name **`adversarial-review`**.

---

## Handoff

**Revised plan + design (revision 2)** ready for review:

- `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- `docs/superpowers/plans/2026-07-15-run-lifecycle.md`

Approve revision 2, then choose execution:

1. Subagent-driven (task-by-task)  
2. Inline with checkpoints
