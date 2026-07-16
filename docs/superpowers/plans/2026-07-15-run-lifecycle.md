# Run lifecycle program — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship durable run lifecycle, honest status, finalization watchdog, isolated review, and optional notifications — covering the full Codex feedback letter in three dense PRs.

**Architecture:** `run.json` + `progress.jsonl` + `envelope.json` remain the durability layer. `target.lifecycle` is source of truth; top-level envelope `status` is a thin projection. Isolation is a review-mode prepare step. Notifications fire only after a validated terminal envelope.

**Tech stack:** Python 3 stdlib wrapper (`plugin/wrapper/scripts/groklib`), Node companion (`plugin/scripts`), unittest + `node --test`.

**Design spec:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md)

**Baseline:** v1.2.10 on `main`.

---

## File map (by PR)

### PR1 — Lifecycle core

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/groklib/runstate.py` | Seed record before run-id emit; atomic write helpers; lifecycle on record |
| `plugin/wrapper/scripts/groklib/progress.py` | `elapsedMs` / phase helpers if needed |
| `plugin/wrapper/scripts/groklib/modes/_shared.py` | Transitions running → finalizing → terminal; watchdog around finalize |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | Same transitions for code/verify path |
| `plugin/wrapper/scripts/groklib/modes/status.py` | Full lifecycle matrix |
| `plugin/wrapper/scripts/groklib/envelope.py` | Document projection; any new error classes in ERROR_CLASSES |
| `plugin/wrapper/scripts/tests/test_mode_status.py` | Matrix tests |
| `plugin/wrapper/scripts/tests/test_runstate.py` or extend existing | Seed ordering, atomic write |
| `plugin/skills/status/SKILL.md` | Transport vs target wording |
| `plugin/wrapper/references/authority-policies.md` | Status row + new error classes |
| `CHANGELOG.md` | Version bump when shipping |

### PR2 — Isolated review

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--isolated` flag |
| `plugin/wrapper/scripts/groklib/modes/review.py` | Require isolation when base/isolated |
| `plugin/wrapper/scripts/groklib/modes/_shared.py` or new `review_isolation.py` | Worktree/snapshot create + fail closed |
| `plugin/wrapper/scripts/tests/test_mode_review.py` | Isolation + isolation-unavailable |
| `plugin/skills/review/SKILL.md`, `adversarial-review/SKILL.md` | Flags |
| `README.md` | Short flag note |
| `CHANGELOG.md` | Ship notes |

### PR3 — Notifications

| Path | Role |
|------|------|
| `plugin/scripts/lib/notify.mjs` (new) | off/auto/native/webhook |
| `plugin/scripts/lib/gate-state.mjs` or jobs config | Persist preference (or sibling state file) |
| `plugin/scripts/grok-companion.mjs` | setup flags; call notify after background terminal envelope |
| `plugin/skills/setup/SKILL.md` | Document prefs |
| `plugin/scripts/tests/notify.test.mjs` (new) | No-op / payload shape / never throws into stdout |
| `CHANGELOG.md`, `docs/RELEASE.md` smoke note | Dual-host |

---

## PR1: Run lifecycle core

### Task 1.1 — Atomic write helpers + seed-before-announce

**Files:**
- Modify: `plugin/wrapper/scripts/groklib/runstate.py`
- Test: `plugin/wrapper/scripts/tests/test_runstate.py` (create if missing) or nearest runstate tests

- [ ] **Step 1: Write failing tests**

```python
def test_create_run_writes_seed_run_json_before_returning():
    paths = runstate.create_run("review")
    record = json.loads((paths.run_dir / "run.json").read_text())
    assert record["runId"] == paths.run_id
    assert record["lifecycle"] in ("created", "running")
    assert record["status"] in ("created", "running")  # keep status field for back-compat

def test_emit_run_id_only_after_seed_exists(monkeypatch, tmp_path):
    # capture stderr; assert last create_run leaves run.json present
    # and marker appears only when file exists
    ...
```

- [ ] **Step 2: Run tests — expect FAIL** (seed not written today)

```bash
cd plugin/wrapper/scripts && python3 -m unittest tests.test_runstate -q
```

- [ ] **Step 3: Implement**

In `create_run` (or a new `create_run_seeded(mode)` used by all modes):

1. mkdir run dir, owner marker, liveness marker, trace dir  
2. **Atomic write** initial `run.json` with:  
   `schemaVersion`, `runId`, `mode`, `createdAtUtc`, `status`/`lifecycle` = `created` (or `running`), null worktree fields, progress/envelope paths  
3. **Then** `emit_run_id_marker(run_id)`  
4. Return `RunPaths`

Add:

```python
def write_json_atomic(path: pathlib.Path, payload: dict) -> None:
    """Write JSON 0600 via temp file + os.replace."""
```

Use for `run.json` and (later) envelope.

- [ ] **Step 4: Tests PASS; commit**

```bash
git commit -m "runstate: seed run.json before advertising run id"
```

### Task 1.2 — Lifecycle field + transition helper

**Files:**
- Modify: `runstate.py`, `_shared.py`, `_worktree.py`, preflight if needed
- Test: unit tests for `transition_lifecycle`

- [ ] **Step 1: Failing tests** for allowed transitions and terminal immutability

```python
ALLOWED = {
  "created": {"running", "failed", "canceled", "interrupted"},
  "running": {"finalizing", "failed", "canceled", "interrupted", "completed"},  # completed only if envelope path short-circuits
  "finalizing": {"completed", "failed", "canceled", "interrupted"},
  ...
}
```

Prefer: `running → finalizing → completed|failed`; allow direct `running → failed` on early errors.

- [ ] **Step 2: Implement `set_lifecycle(paths, lifecycle, **record_updates)`**  
  Reads run.json, updates lifecycle + optional status string for back-compat, atomic write, optional progress emit via callback.

- [ ] **Step 3: Wire mode runners** to call transitions at: start work → `running`; after Grok exit → `finalizing`; after envelope → `completed`/`failed`.

- [ ] **Step 4: Commit**

```bash
git commit -m "runstate: durable target lifecycle transitions"
```

### Task 1.3 — Atomic validated envelope write

**Files:**
- Modify: `envelope.py` and/or `runstate.py` / `_shared.py` emit path
- Test: envelope write then validate round-trip

- [ ] **Step 1: Failing test** — partial write must not leave corrupt final path (simulate by checking replace semantics).

- [ ] **Step 2: Implement `persist_terminal_envelope(paths, envelope) -> None`:**  
  validate → write temp → replace → set lifecycle completed/failed from envelope status.

- [ ] **Step 3: Replace direct envelope path writes in mode success/failure paths.**

- [ ] **Step 4: Commit**

```bash
git commit -m "envelope: atomic terminal persist with validation"
```

### Task 1.4 — Progress elapsedMs + finalizing events

**Files:**
- Modify: `progress.py`, call sites in `_shared.py` finalize block
- Test: progress writer includes elapsedMs when run_started_at passed

- [ ] Emit `finalizing` phase events: sandbox verify, drift report, building envelope, persisted envelope.  
- [ ] Commit: `progress: phase elapsedMs and finalizing events`

### Task 1.5 — Finalization watchdog

**Files:**
- Modify: `_shared.py` (and worktree path) after `grokcli.execute` returns
- New error class: `finalization-timeout` in `envelope.ERROR_CLASSES`
- Test: mock finalize sleep > budget → failure envelope

- [ ] Default budgets: review/reason **120s**, code/verify **180s**; env `GROK_FINALIZE_TIMEOUT_SECONDS` (clamp 30–600).  
- [ ] On timeout: lifecycle failed, persist failure envelope, exit non-zero.  
- [ ] Commit: `modes: finalization watchdog and finalization-timeout`

### Task 1.6 — Status full matrix

**Files:**
- Modify: `modes/status.py`
- Test: `tests/test_mode_status.py`

| Condition | target.lifecycle | top-level status |
|-----------|------------------|------------------|
| process alive, no envelope | `running` or `finalizing` (from run.json) | `running` |
| envelope valid success | `completed` | `success` |
| envelope valid failure | `failed` | `success` (lookup ok) |
| process dead, no envelope | `interrupted` | `success` (lookup ok) |
| envelope malformed | N/A | `failure` + output-malformed |

- [ ] Update `response.target` fields per design §8.  
- [ ] Keep no “stored envelope not found” warning while in-progress.  
- [ ] Dead + no envelope → `interrupted`, not “running”.  
- [ ] Commit: `status: full target lifecycle matrix`

### Task 1.7 — Docs + version for PR1 ship

- [ ] Update `plugin/skills/status/SKILL.md`, authority-policies, CHANGELOG, packaging versions.  
- [ ] Run full Python + Node suites.  
- [ ] Commit + tag patch release for PR1 only when green.

---

## PR2: Isolated review

### Task 2.1 — CLI flag `--isolated`

**Files:** `grok_agent.py`, review arg parsing tests

- [ ] Add `--isolated` boolean (store_true).  
- [ ] Pass through ModeRun / review prepare.  
- [ ] Commit: `cli: add review --isolated`

### Task 2.2 — Isolation required when `--base` or `--isolated`

**Files:** review mode + isolation helper (prefer `plugin/wrapper/scripts/groklib/review_isolation.py` new file to keep review.py thin)

- [ ] **If `--base`:** create external temp worktree at committed revision (reuse worktree helpers where safe; review-only, no code branch naming if possible — or dedicated `grok/review/<run-id>` worktree).  
- [ ] **If `--isolated` without base:** snapshot strategy for tracked dirty state (document: `git stash create` / worktree from HEAD + apply index — pick one implementable approach in code comments; prefer worktree from HEAD + `git checkout` paths that differ).  
- [ ] On any setup failure: `isolation-unavailable`, no live fallback.  
- [ ] Point review cwd/target workspace at isolated tree.  
- [ ] Cleanup isolated tree on success/failure (best-effort; orphan reaper if needed).  

### Task 2.3 — Tests

- [ ] With isolation: create noise file in original checkout during review; must not appear as unexpected run writes; drift warning logic must not hard-fail.  
- [ ] Force isolation mkdir failure → `isolation-unavailable`.  
- [ ] `--base` path always attempts isolation (mock create fail → error).  

### Task 2.4 — Docs + ship

- [ ] review / adversarial-review SKILL argument-hint; README useful flags.  
- [ ] CHANGELOG + version.  
- [ ] Full test suites.

---

## PR3: Notifications + host surface

### Task 3.1 — Preference storage

- [ ] Store `notificationMode`: off|auto|native|webhook and optional `notificationWebhookUrl` next to gate/run-mode state (same privacy posture as gate config).  
- [ ] setup flags: `--notification-mode`, `--notification-webhook`.  
- [ ] Default off; setup help text recommends auto for background.

### Task 3.2 — notify module

**Create:** `plugin/scripts/lib/notify.mjs`

```javascript
export async function notifyRunComplete({ mode, runId, lifecycle, durationSeconds, preference, webhookUrl }) {
  // never throw to caller in a way that fails the job; log stderr only
}
```

- [ ] Payload minimal; redaction not needed if no paths/secrets included.  
- [ ] Tests: off no-ops; native missing binary no-ops; webhook mock fetch.

### Task 3.3 — Wire after terminal

- [ ] Background companion path: after wrapper exits 0/1 and envelope parseable or status terminal, call notify.  
- [ ] Do **not** notify on status command.  
- [ ] Do **not** write notify output to stdout.

### Task 3.4 — Docs + ship

- [ ] setup SKILL, README troubleshooting optional line, RELEASE post-smoke.  
- [ ] CHANGELOG + version.  
- [ ] Dual-host manual smoke: lifecycle status poll + optional notify.

---

## Cross-PR checklist (feedback coverage)

| Feedback item | PR |
|---------------|-----|
| Lifecycle state machine | PR1 |
| Run id after atomic seed record | PR1 |
| Terminal only after atomic validated envelope | PR1 |
| Status table (running / completed / interrupted / failed / canceled) | PR1 |
| Transport vs target result | PR1 |
| Dense phase progress + elapsed | PR1 |
| Finalization watchdog | PR1 |
| Isolated review / no silent fallback | PR2 |
| Optional native/webhook notifications | PR3 |
| Host adapters not chat injection | PR3 (docs) + architecture |
| No broad ignore-list safety | PR2 (explicit non-goal; isolation instead) |

---

## Suggested versioning

| PR | Semver (approx) |
|----|-----------------|
| PR1 | 1.3.0 (behavior + schema fields; minor) |
| PR2 | 1.4.0 (new isolation default for `--base`) |
| PR3 | 1.4.1 or 1.5.0 if setup surface feels feature-y |

Adjust if you prefer a single 1.3.0 only after all three land on a release branch.

---

## Execution notes

- Implement **PR1 completely** before PR2; PR3 can trail.  
- Prefer TDD on status matrix and create_run ordering.  
- Do not reintroduce post-status stderr progress dumps.  
- Keep skill name **`adversarial-review`** (product name); isolation applies to it when it maps to review with `--base`/`--isolated`.  
- Public docs: no process-theater language; product terms only.

---

## Self-review (plan vs design)

| Design § | Covered by |
|----------|------------|
| §6 lifecycle | Task 1.2, 1.6 |
| §7 invariants | Task 1.1, 1.3 |
| §8 progress | Task 1.4 |
| §9 watchdog | Task 1.5 |
| §10 isolation | PR2 tasks |
| §11 notifications | PR3 tasks |
| §12 hosts | PR3 docs + companion |
| §13 error classes | 1.5, 2.2 |
| Three PRs | Structure above |

No TBD placeholders remaining in acceptance criteria.

---

## Handoff

**Plan complete** at:

- Spec: `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- Plan: `docs/superpowers/plans/2026-07-15-run-lifecycle.md`

**Execution options when you are ready to code:**

1. **Subagent-driven** — one subagent per task, review between tasks  
2. **Inline** — execute in this session with checkpoints  

Say which you want, or request edits to the plan first.
