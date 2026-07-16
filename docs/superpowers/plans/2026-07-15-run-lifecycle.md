# Run lifecycle program — Implementation Plan (revision 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Checkboxes track progress.

**Goal:** Full run lifecycle, status projection, process finalize watchdog, isolated review, at-most-once notifications — three PRs, zero open decisions.

**Design:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md) revision 3.

**Baseline:** v1.2.10. **Versions:** 1.3.0 → 1.4.0 → 1.5.0.

**Rule:** Do not invent alternatives. Every step below is mandatory as written.

---

## Projection table (locked)

| Target lifecycle | Top-level status | Exit |
|------------------|------------------|------|
| `created`, `running`, `finalizing` | `running` | 0 |
| `completed` | `success` | 0 |
| `failed`, `canceled`, `interrupted` | `failure` | 1 |
| Load/own/malformed errors | `failure` | 1 |

---

## File map

### PR1 → 1.3.0

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/groklib/runstate.py` | `write_json_atomic`; seed; `set_lifecycle`; `persist_terminal_envelope`; run-id after seed |
| `plugin/wrapper/scripts/groklib/progress.py` | `elapsedMs` on emit |
| `plugin/wrapper/scripts/groklib/modes/_shared.py` | Transitions; spawn finalize worker |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | Same finalize pattern |
| `plugin/wrapper/scripts/groklib/modes/finalize_worker.py` | **New** worker entry `finalize_worker_main` |
| `plugin/wrapper/scripts/groklib/modes/status.py` | Projection + interrupted write |
| `plugin/wrapper/scripts/groklib/envelope.py` | `finalization-timeout` in ERROR_CLASSES |
| `plugin/wrapper/scripts/tests/test_runstate.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_status.py` | Matrix |
| `plugin/wrapper/scripts/tests/test_envelope.py` | exit codes + class |
| `plugin/wrapper/scripts/tests/test_finalize_watchdog.py` | **New** |
| Docs: `plugin/skills/status/SKILL.md`, `plugin/wrapper/references/authority-policies.md`, `plugin/wrapper/SKILL.md`, `README.md`, `docs/COMPATIBILITY.md`, `docs/roadmap.md`, `docs/RELEASE.md`, `CHANGELOG.md`, three packaging version files |

### PR2 → 1.4.0

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--isolated` |
| `plugin/wrapper/scripts/groklib/review_isolation.py` | **New** |
| `plugin/wrapper/scripts/groklib/modes/review.py` | Call isolation |
| `plugin/wrapper/scripts/groklib/envelope.py` | `isolation-unavailable` |
| `plugin/wrapper/scripts/tests/test_review_isolation.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_review.py` | Wire tests |
| Docs: `plugin/skills/review/SKILL.md`, `plugin/skills/adversarial-review/SKILL.md`, `README.md`, `plugin/references/README.md`, `plugin/wrapper/references/authority-policies.md`, `docs/COMPATIBILITY.md`, `docs/roadmap.md`, `CHANGELOG.md`, packaging |

### PR3 → 1.5.0

| Path | Role |
|------|------|
| `plugin/scripts/lib/jobs.mjs` | config fields |
| `plugin/scripts/lib/notify.mjs` | **New** |
| `plugin/scripts/grok-companion.mjs` | setup + background hook |
| `plugin/scripts/tests/notify.test.mjs` | **New** |
| `plugin/scripts/tests/jobs.test.mjs` | prefs |
| Docs: `plugin/skills/setup/SKILL.md`, `README.md`, `docs/RELEASE.md`, `plugin/references/manual-smoke.md`, `docs/COMPATIBILITY.md`, `docs/roadmap.md`, `SECURITY.md`, `CHANGELOG.md`, packaging **1.5.0** |

---

## PR1 — Lifecycle core

### Task 1.1 — Atomic seed before run-id

**Files:** `runstate.py`, `tests/test_runstate.py`

- [ ] **Step 1: Write tests**

```python
class CreateRunSeedTests(unittest.TestCase):
    def test_seed_lifecycle_created_status_running(self):
        paths = runstate.create_run("review")
        record = json.loads((paths.run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["lifecycle"], "created")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["runId"], paths.run_id)
        self.assertEqual(record["mode"], "review")

    def test_write_json_atomic_no_tmp_left(self):
        path = pathlib.Path(self.tmp) / "x.json"
        runstate.write_json_atomic(path, {"a": 1})
        runstate.write_json_atomic(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text())["a"], 2)
        self.assertEqual(list(path.parent.glob("x.json.tmp.*")), [])
```

- [ ] **Step 2: Run — FAIL**

```bash
cd plugin/wrapper/scripts && python3 -m unittest tests.test_runstate -q
```

- [ ] **Step 3: Implement `write_json_atomic` and seed-before-`emit_run_id_marker` in `create_run`** with exact seed fields from design §6.

- [ ] **Step 4: PASS; commit**

```bash
git commit -m "runstate: seed run.json before run-id marker"
```

### Task 1.2 — `set_lifecycle`

**Files:** `runstate.py`, tests in `test_runstate.py`

- [ ] **Step 1: Tests** — allowed edges from design §6; overwrite of `completed` raises `ValueError` or custom error; `interrupted` allowed from any non-terminal.

- [ ] **Step 2: Implement**

```python
def set_lifecycle(paths: RunPaths, lifecycle: str) -> dict:
    # load run.json, check graph, write_json_atomic, return record
```

- [ ] **Step 3: Wire** mode start → `running` after seed (first body line after create_run returns).

- [ ] **Step 4: Commit** `runstate: set_lifecycle transitions`

### Task 1.3 — `persist_terminal_envelope`

**Files:** `runstate.py`, mode emit sites in `_shared.py` / `_worktree.py`

```python
def persist_terminal_envelope(
    paths: RunPaths,
    envelope: dict,
    *,
    lifecycle: str,
) -> None:
    if lifecycle not in ("completed", "failed", "canceled"):
        raise ValueError("lifecycle must be completed|failed|canceled, got {!r}".format(lifecycle))
    violations = envelope_mod.validate_envelope(envelope)
    if violations:
        raise envelope_mod.InvalidEnvelopeError(
            "terminal envelope failed validation",
            {"violations": violations},
        )
    write_json_atomic(paths.envelope_path, envelope)
    set_lifecycle(paths, lifecycle)
```

- [ ] Success path: `lifecycle="completed"`.  
- [ ] Classified failure path: `lifecycle="failed"`.  
- [ ] Cancel path (if any): `lifecycle="canceled"`.  
- [ ] Tests assert lifecycle argument is not inferred from `envelope["status"]` alone.  
- [ ] **Commit** `runstate: persist_terminal_envelope requires lifecycle`

### Task 1.4 — Progress `elapsedMs`

**Files:** `progress.py`, call sites that construct ProgressWriter

- [ ] Store `run_started_monotonic` or parse `createdAtUtc` once; every `emit` adds `elapsedMs: int`.  
- [ ] Parent emits finalizing messages: `"entering finalization"`, `"finalization succeeded"`, `"finalization timed out"`.  
- [ ] **Commit** `progress: elapsedMs on events`

### Task 1.5 — Finalize worker + watchdog

**Files:** `modes/finalize_worker.py` (new), `_shared.py`, `_worktree.py`, `envelope.py`, `tests/test_finalize_watchdog.py`

- [ ] Add `"finalization-timeout"` to `ERROR_CLASSES`.  
- [ ] Implement worker and parent join as design §9 (spawn only, worker persists envelope).  
- [ ] Budgets: design §9 table + env clamp.  
- [ ] Test: mock worker target that sleeps 999 → parent produces finalization-timeout, lifecycle failed, exit 1.  
- [ ] **Commit** `modes: process-based finalization watchdog`

### Task 1.6 — Status projection

**Files:** `status.py`, `test_mode_status.py`

- [ ] Implement design §6 table exactly.  
- [ ] Dead owner + no envelope: `set_lifecycle(interrupted)` best-effort; response lifecycle `interrupted`; top-level `failure`; exit 1.  
- [ ] Valid failure envelope: lifecycle `failed` (from record or envelope); top-level `failure`; exit 1.  
- [ ] In-flight: top-level `running`; exit 0; no missing-envelope warning.  
- [ ] **Commit** `status: locked projection table`

### Task 1.7 — Docs + tag 1.3.0

Mandatory docs list from file map PR1 — **all files**, no optional “if present”.

- [ ] Update every listed doc.  
- [ ] Packaging versions **1.3.0**.  
- [ ] `python3 -m unittest discover -s tests -q` and `node --test tests/*.test.mjs`.  
- [ ] Commit + annotated tag `v1.3.0` + GitHub release notes.

---

## PR2 — Isolated review

### Task 2.1 — Flag

- [ ] `grok_agent.py`: `--isolated` action `store_true`, default False.  
- [ ] Entrypoint test asserts flag present.  
- [ ] **Commit** `cli: add --isolated`

### Task 2.2 — `review_isolation.py`

Implement design §10 exactly.

```python
@dataclass(frozen=True)
class ReviewIsolation:
    worktree_path: pathlib.Path
    diff_path: pathlib.Path | None  # set for --isolated dirty apply; else None

def prepare_review_isolation(
    *,
    repo_root: pathlib.Path,
    run_id: str,
    base: str | None,
    isolated: bool,
) -> ReviewIsolation | None:
    if not base and not isolated:
        return None
    worktree_path = runstate.state_root() / "worktrees" / "review" / run_id
    # git -C repo_root worktree add --detach worktree_path HEAD
    # on failure: raise GrokWrapperError("isolation-unavailable", message, detail)
    # if isolated: write git diff HEAD --binary; git -C worktree apply if non-empty
```

- [ ] **Commit** `review: isolation helper`

### Task 2.3 — Wire review

- [ ] Call prepare when base or isolated.  
- [ ] Failure → failure envelope + `persist_terminal_envelope(..., lifecycle="failed")`.  
- [ ] Success → run against worktree; finally cleanup.  
- [ ] **Commit** `review: enforce isolation for --base and --isolated`

### Task 2.4 — Tests

- [ ] Mock worktree add fail → isolation-unavailable.  
- [ ] Tracked dirty file appears after isolated prepare.  
- [ ] Untracked file does not appear.  
- [ ] Original checkout noise does not force unexpected-edits failure on isolated review.  

### Task 2.5 — Docs + 1.4.0

- [ ] All PR2 docs list files.  
- [ ] Packaging **1.4.0**.  
- [ ] Full suites; tag `v1.4.0`.

---

## PR3 — Notifications

### Task 3.1 — Jobs config

- [ ] Extend save/load defaults: `notificationMode: "off"`, `notificationWebhookUrl: null`.  
- [ ] Export `getNotificationPrefs` / `setNotificationPrefs`.  
- [ ] Tests for default and round-trip.  
- [ ] **Commit** `jobs: notification prefs`

### Task 3.2 — `notify.mjs`

- [ ] Implement design §11 exactly (marker, platforms, webhook).  
- [ ] Windows native: always no-op with reason `windows-native-unsupported`.  
- [ ] Tests: off; already-sent; pending inflight; second call; spawn never uses `shell: true`.  
- [ ] **Commit** `notify: at-most-once adapters`

### Task 3.3 — Companion wire

- [ ] Setup: `--notification-mode <off|auto|native|webhook>`, `--notification-webhook <url>`.  
- [ ] Background terminal → `notifyRunComplete`.  
- [ ] `native` also on foreground terminal.  
- [ ] Never on status.  
- [ ] **Commit** `companion: notification hooks`

### Task 3.4 — Docs + 1.5.0

- [ ] All PR3 docs list files.  
- [ ] Packaging **1.5.0**.  
- [ ] Full suites; tag `v1.5.0`.

---

## Coverage matrix

| Requirement | Task |
|-------------|------|
| Seed before run-id; created+running | 1.1 |
| Lifecycle graph + interrupted write | 1.2, 1.6 |
| Explicit terminal lifecycle | 1.3 |
| Progress elapsed + finalizing | 1.4 |
| Process finalize watchdog | 1.5 |
| Status projection failure for failed/canceled/interrupted | 1.6 |
| Isolation HEAD worktree + base preserved | 2.2–2.3 |
| Dirty tracked apply; no untracked | 2.2 |
| isolation-unavailable | 2.2–2.4 |
| Jobs config notify | 3.1 |
| At-most-once + safe spawn | 3.2 |
| Background/auto rules | 3.3 |
| Full docs | 1.7, 2.5, 3.4 |

---

## Execution order

1. PR1 complete including `v1.3.0`  
2. PR2 complete including `v1.4.0`  
3. PR3 complete including `v1.5.0`  

No parallel tracks. No alternate designs during implementation.

---

## Handoff

Revision **3** locks all decisions. Paths:

- `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- `docs/superpowers/plans/2026-07-15-run-lifecycle.md`  

Ready for approval then execution (subagent-driven or inline).
