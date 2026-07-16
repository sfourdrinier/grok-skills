# Run lifecycle program — Implementation Plan (revision 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Checkboxes track progress.

**Goal:** Full run lifecycle, status projection, process finalize watchdog, isolated review, at-most-once notifications, and verified `code` implementation handoff — **four PRs**, zero open decisions.

**Design:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md) revision 5.

**Baseline:** v1.2.10. **Versions:** 1.3.0 → 1.4.0 → 1.5.0 → **1.6.0**.

**Rule:** Do not invent alternatives. Every step below is mandatory as written. Design §14 is the authority for PR4 schemas and algorithms.

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

## PR4 — Verified implementation handoff (→ 1.6.0)

### File map PR4 (complete — no optional paths)

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--contract-file`; register `handoff` mode |
| `plugin/wrapper/scripts/groklib/implementation_contract.py` | **New** — parse/validate contract JSON; path scope matching |
| `plugin/wrapper/scripts/groklib/implementation_handoff.py` | **New** — patch generation, handoff JSON, ready computation, secret scan |
| `plugin/wrapper/scripts/groklib/modes/code.py` | Load contract; record base SHA; post-Grok HEAD check; scopes; handoff |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | Pass contract; command evidence tails; wire handoff into finalize |
| `plugin/wrapper/scripts/groklib/modes/handoff.py` | **New** — read-only handoff mode |
| `plugin/wrapper/scripts/groklib/modes/cleanup.py` (or existing cleanup module) | Warn on integration-ready handoff |
| `plugin/wrapper/scripts/groklib/envelope.py` | Six new error classes; `MODES` includes `handoff` |
| `plugin/scripts/grok-companion.mjs` | WRAPPER_MODES / STREAMING includes `handoff` |
| `plugin/skills/handoff/SKILL.md` | **New** skill |
| `plugin/skills/handoff/run.mjs` | **New** skill runner (same pattern as code/status) |
| `plugin/skills/code/SKILL.md` | `--contract-file`; pointer to handoff; no auto-integrate |
| `plugin/wrapper/scripts/tests/test_implementation_contract.py` | **New** |
| `plugin/wrapper/scripts/tests/test_implementation_handoff.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_handoff.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_code.py` | unexpected-commit, scope, ready, empty, secrets |
| Docs (all mandatory): `README.md`, `CHANGELOG.md`, `docs/roadmap.md`, `docs/COMPATIBILITY.md`, `docs/RELEASE.md`, `docs/PROVENANCE.md` (if needed), `plugin/references/README.md`, `plugin/references/manual-smoke.md`, `plugin/wrapper/references/authority-policies.md`, `plugin/wrapper/SKILL.md`, packaging **1.6.0**, Claude/Codex manifests that list modes |

### Task 4.1 — Contract module

**Create** `implementation_contract.py` per design §14.3.

- [ ] **Step 1: Tests first** in `test_implementation_contract.py`:

  - Accept valid schemaVersion 1 example from design.  
  - Reject wrong schemaVersion.  
  - Reject bad taskId charset / empty taskId.  
  - Reject absolute path, `..`, empty path, NUL in writeScopes.  
  - `kind: file` exact match only.  
  - `kind: subtree` component match: scope `pkg` matches `pkg/a.ts`, does **not** match `pkg2/a.ts` or string-prefix `pk`.  
  - Empty writeScopes invalid.  
  - requiredValidation: reject shell-string form; require argv list of strings; reject cwd with `..`.  
  - `path_in_scopes(path, scopes)` pure function tests.

- [ ] **Step 2: Implement** parse, validate, `path_in_scopes`, `contract_sha256`.  
- [ ] Classify failures as `implementation-contract-invalid`.  
- [ ] **Commit** `contract: parse and enforce write scopes schema`

### Task 4.2 — Unexpected commit check

**Files:** `code.py` / `_worktree.py` post-Grok

- [ ] Before Grok: resolve and store full `baseRevision` SHA.  
- [ ] After Grok: `git rev-parse HEAD` must equal that SHA.  
- [ ] Else: error `unexpected-commit`; preserve worktree and branch; no `git reset`; no silent commit→handoff; set ready false if handoff still written.  
- [ ] Test: create commit in fixture worktree after “Grok”; assert class and retention.  
- [ ] **Commit** `code: fail unexpected-commit if HEAD moves`

### Task 4.3 — Write-scope enforcement

- [ ] When contract present: every path in post-Grok changed-files set must match a scope.  
- [ ] Violation → `write-scope-violation`; ready false; still attempt forensic patch if safe.  
- [ ] When contract absent: existing worktree confinement only (no new write-scope error).  
- [ ] Tests: file scope miss; subtree miss; multi-scope pass.  
- [ ] **Commit** `code: enforce contract write scopes`

### Task 4.4 — Patch + handoff artifact generation

**Create** `implementation_handoff.py` implementing design §14.5–14.7 **exactly**:

1. Remove validated sentinel `.grok-run-<run-id>`.  
2. Temp index under `runs/<id>/artifacts/handoff.idx`.  
3. `GIT_INDEX_FILE` only for artifact git.  
4. `read-tree` base → `add -A` → `write-tree` → `diff --cached --binary --full-index --no-ext-diff`.  
5. Atomic write patch + handoff JSON mode 0600.  
6. SHA-256 + re-read verify.  
7. Max 25 MiB default; env clamp 1–100 MiB; no truncate.  
8. Secret detector on patch; fail closed.  
9. `compute_integration_ready(...)` checklist from §14.7.

- [ ] Tests in `test_implementation_handoff.py` covering design §14.14 matrix rows for patch shapes, untracked, ignored, sentinel, size limit, hash re-verify, apply→resultTreeOid, permissions 0600.  
- [ ] **Commit** `handoff: immutable git patch and handoff JSON`

### Task 4.5 — Command evidence tails

- [ ] Extend command record builder with stdout/stderr sha256, 4096-byte redacted tails, truncated flags.  
- [ ] Full optional logs under `artifacts/commands/` only if already capturing full streams.  
- [ ] Never dump full command output onto envelope stdout channel.  
- [ ] Tests: bound, redaction, hashes of full content.  
- [ ] **Commit** `commands: bounded redacted output evidence`

### Task 4.6 — Wire code success/failure to handoff + ready

- [ ] On terminal code paths (completed and failed when safe): build handoff artifacts.  
- [ ] `integration.ready` true **only** per design §14.7 (all gates).  
- [ ] Empty changes → ready false, blocker `no-changes`.  
- [ ] Failed gates: forensic patch allowed; ready false; blockers list every reason.  
- [ ] Envelope continues to carry existing C4 fields; handoff JSON is durable artifact on disk.  
- [ ] **Commit** `code: attach implementation handoff on terminal`

### Task 4.7 — Mode `handoff`

- [ ] Register mode in `grok_agent.py`, `envelope.MODES`, companion WRAPPER_MODES.  
- [ ] Implement `modes/handoff.py`: ownership, terminal required, load JSON, rehash patch, schema validate, single envelope.  
- [ ] Error: `artifact-integrity-failure`, `handoff-unavailable`, reuse `state-ownership-violation` as applicable.  
- [ ] Skill `plugin/skills/handoff/SKILL.md` + `run.mjs` (copy runner pattern from status/code).  
- [ ] Argument-hint: `--run-id <id>` only.  
- [ ] Tests: happy path ready true; tamper patch; missing artifact; non-code run; wrong ownership; job id not required; mode performs zero git writes.  
- [ ] **Commit** `handoff: read-only /grok:handoff mode`

### Task 4.8 — Cleanup warning for ready handoff

- [ ] On cleanup dry-run and pre-confirm: if handoff exists with `integration.ready === true`, append clear warning that implementation handoff is unacknowledged / not integrated.  
- [ ] Still allow explicit confirm; never claim cleanup integrated.  
- [ ] **Commit** `cleanup: warn before removing integration-ready handoff run`

### Task 4.9 — Docs + dual-host smoke + 1.6.0

Mandatory docs from file map — **all files**, no optional skip.

Document (design §14.15):

- transfer = conversation context  
- handoff = implementation output  
- neither auto-integrates  
- parent protocol design §14.10 (all 14 steps)  
- parallel peer rules design §14.11  
- wrapper sole authority for readiness  
- notifications ≠ integration success  

Release evidence:

```bash
cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q
cd plugin/scripts && node --test tests/*.test.mjs
claude plugin validate ./plugin --strict
```

Manual dual-host smoke (design §14.16):

1. Claude: code → status → handoff  
2. Codex: code → status → handoff  
3. Failed code → forensic handoff `integration.ready: false`  
4. Tampered patch → integrity failure  
5. Explicit cleanup after handoff inspection  

- [ ] Packaging **1.6.0**; annotated tag `v1.6.0`; GitHub release notes.  

---

## Coverage matrix (full program)

| Requirement | PR / Task |
|-------------|-----------|
| Seed before run-id; created + status running | PR1 / 1.1 |
| Lifecycle + interrupted | PR1 / 1.2, 1.6 |
| Explicit terminal lifecycle (not inferred from status alone) | PR1 / 1.3 |
| Progress elapsed + finalizing | PR1 / 1.4 |
| Process finalize watchdog (spawn) | PR1 / 1.5 |
| Status projection (failure for failed/canceled/interrupted) | PR1 / 1.6 |
| Isolation HEAD + base preserved | PR2 |
| Dirty tracked apply; no untracked | PR2 |
| isolation-unavailable | PR2 |
| Jobs config notify | PR3 |
| At-most-once + safe spawn | PR3 |
| Contract + write scopes + unexpected-commit | PR4 / 4.1–4.3 |
| Immutable patch + handoff JSON + ready | PR4 / 4.4, 4.6 |
| Command evidence tails | PR4 / 4.5 |
| `/grok:handoff` integrity | PR4 / 4.7 |
| Cleanup warn ready handoff | PR4 / 4.8 |
| Full docs + 1.6.0 dual-host smoke | PR4 / 4.9 |
| Full docs each prior ship | PR1–PR3 |

---

## Locked decisions checklist (must remain explicit)

Before claiming “no TBD”, an implementer verifies these are already decided (they are — do not reopen):

| Topic | Decision |
|-------|----------|
| Finalization watchdog | `multiprocessing.get_context("spawn")` + join + terminate/kill (design §9) |
| Dirty-tree isolation | worktree at HEAD + `git diff HEAD --binary` + `git apply`; no untracked (design §10) |
| Notification storage | jobs index config only (design §11) |
| Notify at-most-once | `notified.json` pending/sent (design §11) |
| Status projection | design §6 table / plan projection table |
| Seed | lifecycle `created`, status `running` |
| Terminal lifecycle arg | explicit to `persist_terminal_envelope` |
| Handoff ID | wrapper `runId` only |
| Patch algorithm | temp index + binary full-index diff (design §14.5) |
| Ready gate | wrapper checklist only (design §14.7) |
| Auto-apply | out of scope |

---

## Execution order

1. PR1 → `v1.3.0`  
2. PR2 → `v1.4.0`  
3. PR3 → `v1.5.0`  
4. PR4 → `v1.6.0`  

No parallel tracks. No alternate designs during implementation.

---

## Handoff

Revision **5** expands PR4 to full executable detail from the verified-implementation-handoff feedback; PR1–PR3 remain locked. Paths:

- `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- `docs/superpowers/plans/2026-07-15-run-lifecycle.md`  

Approve, then execute (subagent-driven or inline). **Do not implement PR4 until PR1–PR3 ship unless the user explicitly reorders.**
