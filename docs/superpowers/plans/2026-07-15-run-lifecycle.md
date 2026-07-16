# Run lifecycle program — Implementation Plan (revision 12)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Checkboxes track progress.

**Goal:** Full run lifecycle with CAS, crash-consistent terminal persistence, read-only status, process finalize worker, **opt-in** isolated review, at-most-once notify attempts, verified `code` implementation handoff, and (later) **notify dogfood follow-ups as PR5** — **five PRs**.

**Design:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md) **revision 12**.

**Baseline:** v1.2.10; **PR1 shipped** on main as **1.3.x**. **Versions:** 1.3.x (done) → 1.4.0 → 1.5.0 → 1.6.0 → **1.7.0** (PR5).

**Rule:** Design §4–§14 are authority for PR1–PR4. Rev 9: PR2 isolation is **opt-in** (`--isolated` only). **Rev 10:** operator notify retry is **PR5**. **Rev 11:** quality gates. **Rev 12:** PR5 scope = (A) operator re-attempt, (B) direct-mode completion signal, (C) headless/native honesty — after PR3 dogfood.

**PR1 done. PR2 (opt-in isolation) on branch / review. PR3–PR5 follow execution order + quality gates below.**

---

## Quality gates (mandatory for PR3, PR4, PR5)

Lesson from PR2: short diffs can still be under-hardened. **Green tests without a failure-mode matrix and without internal review is not done.**

### Gate A — Failure-mode matrix (before product code)

Commit or update the PR’s matrix table **in this plan** (or a linked checklist under `docs/superpowers/`) before implementing features. Columns:

| Surface | Happy path | Crash after durable write | Silent wrong outcome | Bypass (direct/host/env) | External knobs | Fail closed / reason | Test id |

Empty cells = **not ready to code**.

### Gate B — DRY (non-negotiable)

| Rule | Meaning |
|------|---------|
| **One implementation** | Shared behavior lives in **one** module/function; call sites only wire. |
| **No copy-paste paths** | If two skills/agents need the same shell prefix, env, or spawn pattern, extract a **single** documented snippet helper or shared markdown fragment — do not retype N variants that can drift. Prefer one canonical include/pattern checked by a test or grep gate. |
| **No dual writers** | One code path writes a durable marker/file (e.g. `notified.json`, handoff manifest); never a second “almost the same” writer. |
| **No dual validators** | Writer and reader of a schema share **one** `validate_*` function. |
| **PR5 reuses PR3** | Operator retry **must** call into `notify.mjs` core (force flag), not reimplement marker/spawn logic. |
| **Review rejects** | Internal review **fails the PR** if it finds duplicated logic that should have been extracted. |

### Gate C — Implementation + tests

- Map every matrix row to a named test (or explicit accepted residual).  
- Crash windows and silent-wrong outcomes are **required** tests, not “nice to have.”  
- Prefer few hardened commits over many “fix review” commits.

### Gate D — Internal code review (part of each PR)

Each of PR3–PR5 **must** include an **internal review task** before packaging/tag:

1. **Spec compliance pass** — matrix complete; design/plan behaviors present; no extra scope.  
2. **Quality pass** — DRY, fail-closed, crash/cleanup, dual-host/direct if applicable, docs match flags.  
3. **Evidence** — write `docs/superpowers/reviews/YYYY-MM-DD-prN-<topic>.md` (or PR comment with the same structure) listing findings + resolutions.  
4. **Zero open remediable findings** — fix or document as accepted residual with owner.  
5. Optional external bot review **after** Gate D, not instead of it.

**Done criteria (PR3–PR5):** Gate A matrix filled · Gate B DRY held · suites green · Gate D review artifact present · packaging only after that · zero open remediable review threads.

### Gate E — Definition of done (release PR)

Packaging triple + CHANGELOG only after Gates A–D. Tag only after merge policy of the branch.

---

## Projection table (locked)

| Effective lifecycle | Top-level status | Exit | Notes |
|---------------------|------------------|------|-------|
| `created`, `running`, `finalizing` | `running` | 0 | From **record** |
| `completed` | `success` | 0 | From record |
| `failed`, `canceled` | `failure` | 1 | From record |
| derived `interrupted` | `failure` | 1 | **In-memory only**; status writes nothing |
| Load/own/malformed errors | `failure` | 1 | |

---

## File maps (exact — no optional paths)

### Packaging version triple (every release PR)

1. `plugin/.claude-plugin/plugin.json`  
2. `plugin/.codex-plugin/plugin.json`  
3. `.claude-plugin/marketplace.json`  

### PR1 → 1.3.0

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/groklib/runstate.py` | seed; CAS; lock; `cas_update_run_record`; `set_lifecycle`; `persist_terminal_envelope`; **delete** public `write_run_record` after migration |
| `plugin/wrapper/scripts/groklib/progress.py` | monotonic `elapsedMs` in owning process |
| `plugin/wrapper/scripts/groklib/modes/_shared.py` | CAS transitions; spawn finalize; freeze modeContext |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | same finalize pattern |
| `plugin/wrapper/scripts/groklib/modes/preflight.py` | preserve seed lifecycle under CAS after `create_run` |
| `plugin/wrapper/scripts/groklib/modes/finalize_worker.py` | **New** — `finalize_worker_main` |
| `plugin/wrapper/scripts/groklib/modes/status.py` | Read-only projection + derived interrupted; **zero writes** |
| `plugin/wrapper/scripts/groklib/envelope.py` | `finalization-timeout`, `finalization-worker-missing-result`, `finalization-worker-unkillable` in ERROR_CLASSES |
| `plugin/wrapper/scripts/tests/test_runstate.py` | **Existing** — extend for seed + CAS |
| `plugin/wrapper/scripts/tests/test_mode_status.py` | Projection + byte-identical run dir |
| `plugin/wrapper/scripts/tests/test_mode_cleanup.py` | Fixture migration for seed/CAS |
| `plugin/wrapper/scripts/tests/test_mode_review.py` | Fixture migration |
| `plugin/wrapper/scripts/tests/test_envelope.py` | exit codes + class |
| `plugin/wrapper/scripts/tests/test_finalize_watchdog.py` | **New** — races + single terminal writer |
| `plugin/skills/status/SKILL.md` | Exit 1 = target failure may still yield envelope; always relay JSON |
| `plugin/wrapper/references/authority-policies.md` | lifecycle + status read-only |
| `plugin/wrapper/SKILL.md` | status + lifecycle |
| `README.md` | status projection |
| `docs/COMPATIBILITY.md` | status exit / projection |
| `docs/roadmap.md` | 1.3.0 |
| `docs/RELEASE.md` | smoke including failed-target status |
| `CHANGELOG.md` | 1.3.0 |
| Packaging triple | **1.3.0** |

**Not in PR1:** `docs/PROVENANCE.md` (no edit).

### PR2 → 1.4.0 (opt-in isolation)

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--isolated` store_true (default off) |
| `plugin/wrapper/scripts/groklib/review_isolation.py` | **New** — prepare + ownership + dirty apply + cleanup |
| `plugin/wrapper/scripts/groklib/modes/review.py` | Call isolation **only when `--isolated`** |
| `plugin/wrapper/scripts/groklib/envelope.py` | `isolation-unavailable` |
| `plugin/wrapper/scripts/tests/test_review_isolation.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_review.py` | Wire + concurrent + partial cleanup; **live path without `--isolated`** |
| `plugin/skills/review/SKILL.md` | document opt-in `--isolated`; `--base` alone stays live |
| `plugin/skills/adversarial-review/SKILL.md` | same opt-in policy as review |
| `README.md` | isolation opt-in |
| `plugin/references/README.md` | isolation opt-in |
| `plugin/wrapper/references/authority-policies.md` | isolation opt-in |
| `docs/COMPATIBILITY.md` | |
| `docs/roadmap.md` | 1.4.0 |
| `CHANGELOG.md` | 1.4.0 |
| Packaging triple | **1.4.0** |

**Not in PR2:** `docs/PROVENANCE.md`. **Not isolation triggers:** `--base` alone, adversarial-review default.

### PR3 → 1.5.0

| Path | Role |
|------|------|
| `plugin/scripts/lib/jobs.mjs` | notificationMode + webhookUrl defaults |
| `plugin/scripts/lib/notify.mjs` | **New** — at-most-once **attempt** |
| `plugin/scripts/grok-companion.mjs` | read `GROK_COMPANION_EXECUTION_CONTEXT`; notify hooks; never forward context to wrapper |
| `plugin/scripts/lib/skill-run.mjs` | **No functional change** (skills/agents set env in shell before `node …/run.mjs`) |
| `plugin/skills/code/SKILL.md` | Prefix env on wait vs background companion invocations |
| `plugin/skills/review/SKILL.md` | same |
| `plugin/skills/reason/SKILL.md` | same |
| `plugin/skills/adversarial-review/SKILL.md` | same |
| `plugin/skills/verify/SKILL.md` | same |
| `plugin/agents/grok-engineer-coder.md` | **Always** prefix `GROK_COMPANION_EXECUTION_CONTEXT` on every companion/`agents/run.mjs` invocation (foreground default unless background chosen) |
| `plugin/agents/grok-rescue.md` | **Always** prefix env on every invocation: rescue runs `reason` and optionally `code` — both are live modes that can notify under `native`/`auto` |
| `plugin/codex-agents/grok-engineer-coder.toml` | Same always-prefix env rule in materialization/docs for Codex |
| `plugin/codex-agents/grok-rescue.toml` | Same always-prefix env rule |
| `plugin/scripts/tests/notify.test.mjs` | **New** |
| `plugin/scripts/tests/jobs.test.mjs` | prefs |
| `plugin/scripts/tests/grok-companion.test.mjs` | context + notify paths (foreground + background) |
| `plugin/skills/setup/SKILL.md` | notification flags |
| `README.md` | notify |
| `docs/RELEASE.md` | notify smoke |
| `plugin/references/manual-smoke.md` | notify |
| `docs/COMPATIBILITY.md` | notify + execution context |
| `docs/roadmap.md` | 1.5.0 |
| `SECURITY.md` | webhook notify surface |
| `CHANGELOG.md` | 1.5.0 |
| `docs/superpowers/reviews/*-pr3-*.md` | Failure-mode matrix + internal review artifact |
| Packaging triple | **1.5.0** |

**Not in PR3:** changes to `plugin/scripts/lib/skill-run.mjs` behavior (locked no-op).  
**Not in PR3 (→ PR5 or permanent non-goal):**
- Operator notify re-attempt (**PR5-A**)
- Direct-mode push notify / job-side marker home (**PR5-B**)
- Headless/native honesty (setup + docs; optional native-fail hint) (**PR5-C**)
- Automatic retry of `pending`/`failed` (permanent non-goal)
- Exactly-once / guaranteed delivery (permanent non-goal)

### PR4 → 1.6.0

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--contract-file`; register `handoff` |
| `plugin/wrapper/scripts/groklib/implementation_contract.py` | **New** |
| `plugin/wrapper/scripts/groklib/implementation_handoff.py` | **New** — patch, two-phase manifest, `validate_implementation_handoff`, ready |
| `plugin/wrapper/scripts/groklib/modes/code.py` | contract; order §14.6; validation exec; handoff phases |
| `plugin/wrapper/scripts/groklib/modes/_worktree.py` | wire code finalization order + command evidence |
| `plugin/wrapper/scripts/groklib/modes/handoff.py` | **New** — read-only |
| `plugin/wrapper/scripts/groklib/modes/cleanup.py` | factual ready-handoff warning (§14.17) |
| `plugin/wrapper/scripts/groklib/envelope.py` | **seven** PR4 error classes in ERROR_CLASSES + MODES += `handoff` (see list below) |
| `plugin/scripts/grok-companion.mjs` | WRAPPER_MODES += `handoff`; **not** STREAMING; `runHandoff()` |
| `plugin/skills/handoff/SKILL.md` | **New** |
| `plugin/skills/handoff/run.mjs` | **New** |
| `plugin/skills/code/SKILL.md` | contract-file; handoff pointer; one target |
| `plugin/wrapper/scripts/tests/test_implementation_contract.py` | **New** |
| `plugin/wrapper/scripts/tests/test_implementation_handoff.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_handoff.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_code.py` | order, blockers, ready, operator-trusted validation |
| `plugin/scripts/tests/grok-companion.test.mjs` | handoff non-streaming / no job |
| Docs: `README.md`, `CHANGELOG.md`, `docs/roadmap.md`, `docs/COMPATIBILITY.md`, `docs/RELEASE.md`, `plugin/references/README.md`, `plugin/references/manual-smoke.md`, `plugin/wrapper/references/authority-policies.md`, `plugin/wrapper/SKILL.md` | all mandatory |
| `docs/superpowers/reviews/*-pr4-*.md` | Failure-mode matrix + internal review artifact |
| Packaging triple | **1.6.0** |
| Claude/Codex manifests | packaging triple only (modes discovered from skills dirs; no separate mode list file) |

**Not in PR4:** `docs/PROVENANCE.md`.

**PR4 envelope ERROR_CLASSES (exactly seven):**

```text
implementation-contract-invalid
write-scope-violation
unexpected-commit
artifact-generation-failure
artifact-integrity-failure
handoff-unavailable
terminal-envelope-incomplete
```

(`temp-index-retained` is a handoff **blocker** string, not a separate ERROR_CLASSES entry.)

---

## PR1 — Lifecycle core

### /goal strings (copy-paste)

Use these under `/goal` when executing PR1. One goal at a time: prefer the **task** goal while coding that task; use the **PR1 ship** goal for end-to-end finish.

**PR1 ship (full release gate):**

```text
Ship PR1 lifecycle core as v1.3.0 on branch feat/pr1-run-lifecycle-1.3.0 per design+plan rev 8. Done only when ALL: Tasks 1.1–1.7 complete; seed before run-id + CAS/run.lock + envelope-first persist_terminal_envelope + read-only status (envelope-aware projection) + monotonic elapsedMs + spawn finalize worker with parent recovery only when is_alive() is False; cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q exits 0; cd plugin/scripts && node --test tests/*.test.mjs exits 0; packaging triple 1.3.0; PR1 docs list updated; no PR2–PR4 product scope.
```

| Task | `/goal` objective (paste after `/goal `) |
|------|------------------------------------------|
| **1.1** | `PR1 Task 1.1: create_run seeds run.json (lifecycle=created, status=running, recordRevision=0) before emit_run_id_marker; inventory all create_run callers; migrate preflight/_shared/_worktree + fixtures off full-replace write_run_record toward CAS merge; tests pass for seed + migration; commit "runstate: seed run.json with recordRevision before run-id marker". Done when those tests green and commit exists on feat/pr1-run-lifecycle-1.3.0.` |
| **1.2** | `PR1 Task 1.2: implement run.lock + cas_update_run_record + set_lifecycle with design §6 graph and recordRevision CAS; terminal lifecycle overwrite refused; concurrent CAS conflict tested; commit "runstate: CAS recordRevision and run.lock". Done when unit tests green and commit exists.` |
| **1.3** | `PR1 Task 1.3: persist_terminal_envelope envelope-first per design §7.1; idempotent lifecycle finish if valid envelope exists; never replace terminal envelope body; crash-after-envelope-before-lifecycle test; success/failure/cancel paths; commit "runstate: envelope-first crash-consistent terminal persist". Done when those tests green and commit exists.` |
| **1.4** | `PR1 Task 1.4: ProgressWriter elapsedMs from process-local monotonic clock; UTC ts on events; worker does not write progress; parent finalizing messages; status display elapsed from UTC with clamp; commit "progress: monotonic elapsedMs in owning process". Done when tests green and commit exists.` |
| **1.5** | `PR1 Task 1.5: spawn finalize_worker per design §9/§9.4; worker normal terminal writer; parent durable recovery only when is_alive() is False; durable classes finalization-timeout/cli-failure/finalization-worker-missing-result; unkillable → ephemeral only; race tests; commit "modes: process finalize worker with confirmed-dead parent recovery". Done when test_finalize_watchdog green and commit exists.` |
| **1.6** | `PR1 Task 1.6: status strictly read-only; effective lifecycle record/envelope/derived per design §6; byte-identical run dir after status; failed target exit 1 with envelope relay; status SKILL.md updated; commit "status: read-only projection with envelope-aware effective lifecycle". Done when test_mode_status green and commit exists.` |
| **1.7** | `PR1 Task 1.7: all PR1 docs from file map; packaging triple 1.3.0; full Python+Node suites green; commit and annotated tag v1.3.0. Done when versions are 1.3.0, suites pass, and tag exists.` |

**Session rule:** When a task goal completes, mark `/goal` completed (or clear and set the next task goal). Do not start Task N+1 until Task N’s commit exists unless the plan requires a single combined commit (it does not).

### Task 1.1 — Atomic seed before run-id + caller inventory

**/goal:** see table row **1.1** above.

**Files:** `runstate.py`, `preflight.py`, `_shared.py`, `_worktree.py`, `test_runstate.py`, fixture tests listed in file map

- [ ] **Step 1: Tests**

```python
def test_seed_lifecycle_created_status_running_revision_zero(self):
    paths = runstate.create_run("review")
    record = json.loads((paths.run_dir / "run.json").read_text(encoding="utf-8"))
    self.assertEqual(record["lifecycle"], "created")
    self.assertEqual(record["status"], "running")
    self.assertEqual(record["recordRevision"], 0)
    self.assertEqual(record["runId"], paths.run_id)

def test_emit_run_id_only_after_seed_exists(self):
    # create_run must leave run.json before marker emission; unit-test order via spy or file mtime sequence
    ...
```

- [ ] Inventory every `create_run()` caller (design §6) and update each.  
- [ ] Replace full-replacement dumps: modes use `cas_update_run_record` merge only.  
- [ ] Preflight explicitly preserves lifecycle / createdAtUtc / recordRevision.  
- [ ] Migration tests for preflight, review/reason path, code/verify path, cleanup fixtures, status fixtures.  
- [ ] **Commit** `runstate: seed run.json with recordRevision before run-id marker`

### Task 1.2 — Lock + CAS API

**/goal:** see table row **1.2** above.

**Files:** `runstate.py`, tests

- [ ] Implement exclusive `run.lock` (fcntl Unix / msvcrt Windows).  
- [ ] `cas_update_run_record(paths, expected_revision, patch)`.  
- [ ] `set_lifecycle(paths, expected_revision, lifecycle)` with design graph.  
- [ ] Overwrite of terminal lifecycle refuses write.  
- [ ] Concurrent CAS conflict raises / returns conflict (tests).  
- [ ] **Commit** `runstate: CAS recordRevision and run.lock`

### Task 1.3 — Crash-consistent `persist_terminal_envelope`

**/goal:** see table row **1.3** above.

Implement design §7.1 exactly (envelope-first; idempotent lifecycle finish).

```python
def persist_terminal_envelope(
    paths: RunPaths,
    expected_revision: int,
    envelope: dict | None,
    *,
    lifecycle: str | None,
) -> None:
    # under lock per §7.1:
    # if valid envelope exists → finish lifecycle only; never replace body
    # else write envelope.json FIRST, then CAS lifecycle SECOND
```

- [ ] Success / failure / cancel paths.  
- [ ] Test: second different envelope does not replace first.  
- [ ] Test: crash after envelope before lifecycle → recovery finishes lifecycle; envelope unchanged.  
- [ ] Test: lifecycle argument never inferred from envelope alone when writing **new** envelope (caller still passes it).  
- [ ] **Commit** `runstate: envelope-first crash-consistent terminal persist`

### Task 1.4 — Progress `elapsedMs` (monotonic owner)

**/goal:** see table row **1.4** above.

- [ ] ProgressWriter stores `time.monotonic()` start at construction (owning process).  
- [ ] Every emit includes `elapsedMs` and UTC `ts`.  
- [ ] Worker does not write progress.  
- [ ] Parent emits finalizing messages: entering / succeeded / timed out.  
- [ ] Status derives display elapsed from UTC/`createdAtUtc` when needed; clamp negative to 0.  
- [ ] **Commit** `progress: monotonic elapsedMs in owning process`

### Task 1.5 — Finalize worker protocol

**/goal:** see table row **1.5** above.

**Files:** `modes/finalize_worker.py`, `_shared.py`, `_worktree.py`, `envelope.py`, `tests/test_finalize_watchdog.py`

Implement design §9 and §9.4 exactly:

- Serializable `finalize-payload.json` only.  
- Worker = normal terminal writer via `persist_terminal_envelope`.  
- Parent durable recovery **only when `proc.is_alive() is False`** (confirmed). Timed join is not proof of death.  
- Parent-authorized durable failure classes only: `finalization-timeout`, `cli-failure`, `finalization-worker-missing-result`.  
- If still alive after kill grace: **no durable write**; ephemeral stdout `finalization-worker-unkillable`; lifecycle stays `finalizing`.  
- Parent never writes success envelopes.  
- Idempotent lifecycle finish when envelope already valid (only if not alive).  

Tests:

- [ ] Worker completes before timeout → success preserved.  
- [ ] Worker completes during kill window → envelope preserved.  
- [ ] Worker completes after parent would have written timeout → no replacement.  
- [ ] True hang that dies after kill → finalization-timeout once; lifecycle failed.  
- [ ] Unkillable worker (mock still alive) → no durable envelope; ephemeral unkillable; lifecycle finalizing.  
- [ ] Nonzero worker exit without envelope → cli-failure.  
- [ ] Exit 0 without envelope → finalization-worker-missing-result.  
- [ ] Parent durable-write guard requires `is_alive() is False`.  
- [ ] Spawn payload has no non-serializable fields.  
- [ ] **Commit** `modes: process finalize worker with confirmed-dead parent recovery`

### Task 1.6 — Status projection (read-only)

**/goal:** see table row **1.6** above.

**Files:** `status.py`, `test_mode_status.py`, `plugin/skills/status/SKILL.md`

- [ ] Projection table + effective lifecycle resolution design §6 (record / envelope / derived).  
- [ ] Dead owner + no envelope → **derived** `interrupted`; **no** writes.  
- [ ] Valid envelope + non-terminal record → effective lifecycle from envelope; **no** writes.  
- [ ] Test: recursive content hash of run dir identical before/after status.  
- [ ] Valid failure envelope → top-level failure, exit 1, envelope relayed.  
- [ ] Skill text: exit 1 can mean inspected failed target; always relay JSON; distinguish parse failure.  
- [ ] **Commit** `status: read-only projection with envelope-aware effective lifecycle`

### Task 1.7 — Docs + tag 1.3.0

**/goal:** see table row **1.7** above.

- [ ] All PR1 docs from file map.  
- [ ] Packaging triple **1.3.0**.  
- [ ] Full Python + Node suites.  
- [ ] Commit + annotated tag `v1.3.0`.

---

## PR2 — Opt-in isolated review

**Policy (rev 9):** Isolation runs **only** when the operator/agent passes `--isolated`.  
`--base` alone remains **live checkout** (comparison framing only). When `--isolated` is set, setup failures are **fail closed** (`isolation-unavailable`) — no silent fallback to live tree.

### Task 2.1 — Flag

- [x] `grok_agent.py`: `--isolated` store_true, **default false**.  
- [x] **Commit** `cli: add opt-in --isolated`

### Task 2.2 — `review_isolation.py`

Implement design §10 exactly (only used when isolation is requested):

- Owner marker sibling `{worktree_path}.owner.json`.  
- Never reuse existing path.  
- Dirty: `git diff --no-ext-diff --no-textconv --binary --full-index --ita-invisible-in-index <pinned-base-sha> --` from repo root; apply in worktree; reject dirty submodules.  
- Cleanup always: remove worktree, prune, marker, diff.  

- [x] **Commit** `review: isolation helper with ownership`

### Task 2.3 — Wire review

- [x] Call isolation helper **iff** `args.isolated` (or equivalent) is true.  
- [x] `--base` without `--isolated` → **no** isolation path.  
- [x] Isolation failure → failure envelope via terminal writer (`isolation-unavailable`).  
- [x] finally cleanup always when isolation was started.  
- [x] **Commit** `review: opt-in isolation via --isolated only`

### Task 2.4 — Tests

- [x] Without `--isolated`: live review path unchanged (including with `--base`).  
- [x] With `--isolated`: worktree add fail → isolation-unavailable.  
- [x] Tracked dirty (staged+unstaged) appears; untracked does not.  
- [x] `git add -N` intent-to-add does **not** appear in isolated tree.  
- [x] Submodule dirty rejected.  
- [x] Apply failure → isolation-unavailable (no live fallback).  
- [x] Concurrent isolated runs; partial cleanup.  
- [x] Isolated run: original checkout noise does not force unexpected-edits.

### Task 2.5 — Docs + 1.4.0

- [x] All PR2 docs; packaging triple **1.4.0**; suites; tag `v1.4.0` (tag on merge to main).  
- [x] Explicit docs: opt-in only; when to use `--isolated` vs live `--base`.

---

## PR3 — Notifications (→ 1.5.0)

**Authority:** design §11. **Quality:** Gates A–E above.  
**DRY:** Single `notify.mjs` owns marker + adapters; companion only decides *whether* to call; skills share **one** env-prefix pattern (not N divergent shell lines).

### Task 3.0 — Failure-mode matrix + DRY plan (Gate A)

Fill before any product code. Minimal rows (expand if needed):

| Surface | Crash / silent wrong | Bypass | Fail closed | Test |
|---------|----------------------|--------|-------------|------|
| `notified.json` create pending | Crash after pending before send → no auto-retry | Double companion completion | `already-attempted` | yes |
| `notified.json` complete | Crash after send before complete → no auto-retry | — | next auto path skips | yes |
| `off` | Never notify | misconfig | no-op | yes |
| `auto` + FG | No native notify | missing context → FG default | no-op | yes |
| `auto` + BG | Notify once | — | — | yes |
| `native` FG/BG | Notify once each path | adapter missing | completed+failed | yes |
| `webhook` | POST once; timeout | bad URL | completed+failed; job not failed | yes |
| status/result/jobs/setup/handoff | Never notify | — | — | yes |
| Wrapper env | Context never on wrapper child | — | strip/ignore | yes |
| skill-run.mjs | Unchanged | accidental edit | review + test | yes |
| Native spawn | shell false only | — | — | yes |

- [x] Matrix: `docs/superpowers/reviews/2026-07-16-pr3-notifications-matrix.md`.  
- [x] Single modules: `notify.mjs`, `jobs.mjs` defaults, companion thin hook.  
- [x] Committed on feature branch.

### Task 3.1 — Jobs config

- [x] Defaults off / null via `DEFAULT_JOBS_CONFIG`.  
- [x] Committed.

### Task 3.2 — `notify.mjs` at-most-once attempt (only writer)

Design §11: exclusive pending; already-attempted; complete marker; shell false; not exactly-once.

- [x] Implemented + `notify.test.mjs`.  
- [x] Committed.

### Task 3.3 — Companion hooks + execution context

- [x] `wrapperChildEnv`; `maybeNotifyAfterTerminal`; eligible modes; skill-run unchanged.  
- [x] Committed.

### Task 3.4 — Skill/agent env prefix (DRY)

- [x] `plugin/references/execution-context.md` + skills/agents/codex-agents.  
- [x] Committed.

### Task 3.5 — Internal code review (Gate D)

- [x] `docs/superpowers/reviews/2026-07-16-pr3-full-review.md` (+ matrix/internal).  
- [x] Includes PR2 late isolation carry-forward.  
- [x] Zero open remediable findings.

### Task 3.6 — Docs + 1.5.0 (Gate E)

- [x] Packaging **1.5.0**; CHANGELOG; COMPATIBILITY; RELEASE; SECURITY; manual-smoke; references.  
- [x] Docs: at-most-once only; operator retry = PR5.  
- [ ] Tag `v1.5.0` after merge to main.

---

## PR4 — Verified implementation handoff (→ 1.6.0)

**Authority:** design §14. **Quality:** Gates A–E.  
**DRY:** One contract parser; one handoff validator used by writer **and** `handoff` mode; one finalization order function; command evidence helper shared by all gate commands.

### Task 4.0 — Failure-mode matrix + DRY plan (Gate A)

Minimal rows:

| Surface | Crash / silent wrong | Fail closed | Test |
|---------|----------------------|-------------|------|
| Finalization order §14.6 | Step skipped / reordered | assert order | yes |
| Sentinel | Missing/symlink/user path | blocker | yes |
| Unexpected commit | HEAD ≠ base | blocker; no reset | yes |
| Write scopes | Escape / prefix confusion | blocker; forensics continue when safe | yes |
| Temp index | Left on disk | `temp-index-retained` | yes |
| Temp index delete race | Delete err but gone | warning only | yes |
| requiredValidation | shell=true / cwd escape | reject; no OS-sandbox claim | yes |
| Original checkout dirty after validation | — | ready false | yes |
| Manifest then envelope | Crash between | dual-condition handoff not ready | yes |
| Rewrite ready-true after terminal | — | forbidden | yes |
| `/grok:handoff` | Spawns Grok | must not | yes |

- [ ] Matrix committed.  
- [ ] Name single modules: contract parse, patch builder, validate_implementation_handoff, run_post_grok_finalization (order), command_evidence.  
- [ ] **Commit** `docs: PR4 failure-mode matrix and DRY boundaries`

### Task 4.1 — Contract module

**Create** `implementation_contract.py` per design §14.3.

- [ ] Parse/validate schemaVersion, taskId, target, scopes, requiredValidation argv.  
- [ ] `path_in_scopes` component semantics.  
- [ ] Classify `implementation-contract-invalid`.  
- [ ] Trust model in module docstring.  
- [ ] Tests: prefix confusion, traversal, absolute, empty scopes.  
- [ ] **Commit** `contract: parse write scopes and validation descriptors`

### Task 4.2 — Unexpected commit as blocker

- [ ] After Grok: HEAD must equal base; else blocker `unexpected-commit`; no reset; continue if readable.  
- [ ] **Commit** `code: unexpected-commit blocker without aborting forensics`

### Task 4.3 — Finalization order + write scopes

Implement design §14.6 order in **one** function (no copy-pasted step lists in code/verify):

```text
verify sentinel → remove exact sentinel → HEAD check → changed files
→ write scopes → forensic patch → requiredValidation → build gate
→ shared safety → ready → final handoff JSON → terminal envelope
```

- [ ] Sentinel never in changed files/patch.  
- [ ] Malformed/missing/symlink sentinel fails.  
- [ ] Cannot remove user-authored similarly named path.  
- [ ] Scope violation → blocker + continue forensics when safe.  
- [ ] Test asserts step order.  
- [ ] **Commit** `code: locked post-Grok finalization order`

### Task 4.4 — Phase-1 forensic patch

**Create** `implementation_handoff.py` phase 1: unique temp index; `finally` delete + post-check §14.7; binary full-index patch; size limit; secret scan; 0600/0700.

- [ ] Index still exists → `temp-index-retained`, ready false.  
- [ ] Delete errors but path absent → warning only.  
- [ ] Tests: add/modify/delete/rename/binary/symlink/mode; untracked in; ignored out; sentinel out; `-z` odd paths; both cleanup cases.  
- [ ] Apply to base → resultTreeOid.  
- [ ] **Commit** `handoff: phase-1 immutable git patch`

### Task 4.5 — Execute contract validation (operator-trusted)

- [ ] Run each requiredValidation after scopes + HEAD.  
- [ ] cwd inside worktree; reject escape.  
- [ ] shell=False; **no OS FS sandbox claim**.  
- [ ] Post-command original-checkout unmodified.  
- [ ] Evidence before interpreting exit.  
- [ ] Nonzero → blocker; ready false.  
- [ ] `trustModel` = `operator-contract-trusted-no-os-sandbox`.  
- [ ] Tests: cwd escape; shell not used; checkout dirty blocks ready.  
- [ ] **Do not** claim OS “cannot write outside worktree.”  
- [ ] **Commit** `code: execute operator-trusted contract requiredValidation`

### Task 4.6 — Command evidence tails

- [ ] **One** helper: sha256 + 4096 redacted tails + truncated flags.  
- [ ] Never full logs on envelope stdout.  
- [ ] **Commit** `commands: bounded redacted evidence`

### Task 4.7 — Phase-2 handoff + ready from terminalOutcome

- [ ] In-memory `terminalOutcome`; ready per §14.12 (not disk lifecycle).  
- [ ] Final `implementation-handoff.json` **before** `persist_terminal_envelope`.  
- [ ] Envelope-first terminal persist.  
- [ ] **`validate_implementation_handoff` single function** used by writer.  
- [ ] validation.sources §14.10.  
- [ ] Never rewrite ready-true after terminal envelope published.  
- [ ] Tests: sandbox/build/validation fail; success ready; multi-blocker; empty; crash between manifest/envelope; crash between envelope/lifecycle.  
- [ ] **Commit** `handoff: phase-2 manifest from terminalOutcome`

### Task 4.8 — Mode `handoff` (non-streaming)

- [ ] WRAPPER_MODES only; not STREAMING_MODES.  
- [ ] `runHandoff()` like status.  
- [ ] **Same** `validate_implementation_handoff` as writer (DRY).  
- [ ] Dual-condition ready §14.12.  
- [ ] Rehash patch; integrity; unavailable; `terminal-envelope-incomplete`.  
- [ ] Skill + run.mjs.  
- [ ] Tests: no Grok; no companion job; read-only; dual-condition ready.  
- [ ] **Commit** `handoff: read-only non-streaming /grok:handoff`

### Task 4.9 — Cleanup factual warning

- [ ] Exact meaning design §14.17 — no “unacknowledged.”  
- [ ] **Commit** `cleanup: warn on integration-ready handoff removal`

### Task 4.10 — Internal code review (Gate D)

- [ ] Spec + quality pass (order, dual-condition ready, DRY validators, no OS-sandbox lies).  
- [ ] Artifact `docs/superpowers/reviews/YYYY-MM-DD-pr4-handoff.md`.  
- [ ] Zero open remediable findings.  
- [ ] **Commit** `review: PR4 internal review artifact`

### Task 4.11 — Docs + dual-host smoke + 1.6.0 (Gate E)

- [ ] All PR4 docs from file map.  
- [ ] Parent protocol + transfer vs handoff.  
- [ ] Path headers / skill frontmatter.  
- [ ] Suites + `claude plugin validate ./plugin --strict`.  
- [ ] Dual-host smoke §14.19.  
- [ ] Packaging triple **1.6.0**; tag after merge.  
- [ ] **Commit** `release: 1.6.0 implementation handoff`

---

## PR5 — Notify dogfood follow-ups (→ 1.7.0)

**When:** After **PR3 shipped and dogfooded** (typically after PR4). Does not block 1.5.0/1.6.0.

**Product (three tracks):**

| Track | Name | Why |
|-------|------|-----|
| **PR5-A** | Operator re-attempt | Recover from failed/stuck notify without auto-retry |
| **PR5-B** | Direct-mode completion signal | Direct has no durable `runs/<id>`; still needs a marker home for push |
| **PR5-C** | Headless / native honesty | Setup + docs (and optional native-fail hint) so operators prefer webhook off-desktop |

### Policy (locked, all tracks)

| Rule | Decision |
|------|----------|
| Automatic retry of `pending` / `failed` | **Still never** |
| Exactly-once delivery | **Still not claimed** |
| Who may re-fire (A) | **Operator only**; may duplicate |
| Failure of notify / retry | Must not fail/reopen terminal run outcome |
| **DRY** | Reuse PR3 `notify.mjs` + `notification-modes.mjs`; **no** second marker/spawn stack |
| Direct marker home (B) | Prefer **job-scoped** marker under companion jobs state (not inventing fake wrapper runs) |
| Headless (C) | Do not claim native works without a desktop session |

### Task 5.0 — Failure-mode matrix + DRY proof (Gate A)

| Surface | Silent wrong | Test |
|---------|--------------|------|
| Auto path after PR5 | Must never call force | yes |
| Operator force after failed | Re-fire once; may duplicate | yes |
| Stuck `pending` | No auto-fire; operator force only if policy allows + docs | yes |
| Terminal outcome | Unchanged by retry / direct notify | yes |
| Direct without job id | Fail closed or skip; never invent wrapper run dir | yes |
| Direct + hardened | Separate marker roots; no cross-write | yes |
| Native headless | Setup/docs honest; optional stderr hint | yes |
| Duplicate adapter code | Forbidden (DRY) | review |

- [ ] Matrix committed (covers A+B+C).  
- [ ] **Commit** `docs: PR5 failure-mode matrix`

### Task 5.1 — Operator re-attempt API (PR5-A; extends notify.mjs only)

- [ ] Explicit `{ force: true }` / CLI; not used by automatic completion.  
- [ ] States documented: `completed`+`failed` re-fire; stuck `pending` policy.  
- [ ] Prefer overwrite with `lastAttempt` fields (no unbounded history unless required).  
- [ ] Tests prove **zero** new spawn/marker helpers outside `notify.mjs`.  
- [ ] **Commit** `notify: operator re-attempt (may duplicate)`

### Task 5.2 — Companion / skill surface for retry (PR5-A thin)

- [ ] Operator invocation only; never from auto completion path.  
- [ ] Thin wrapper → `notify.mjs` force API.  
- [ ] **Commit** `companion: notify-retry operator path`

### Task 5.3 — Direct-mode completion signal (PR5-B)

- [ ] When `runMode === "direct"` and prefs would notify, use a **job-scoped** marker home (e.g. under jobs state for that job id), not wrapper `runs/`.  
- [ ] At-most-once same as hardened (`wx` / pending → completed).  
- [ ] Payload still mode/lifecycle/job-or-run id/duration; document direct vs hardened marker paths.  
- [ ] Tests: direct + auto/native/webhook; second attempt blocked; never creates wrapper runs dir.  
- [ ] **Commit** `notify: direct-mode job-scoped completion signal`

### Task 5.4 — Headless / native honesty (PR5-C)

- [ ] Setup report + hints: `native`/`auto` need a desktop session; recommend `webhook` for SSH/CI/headless.  
- [ ] On native send failure with no-display-shaped detail, stderr one-line hint toward webhook (best-effort).  
- [ ] **Windows:** keep native as **unsupported** (`windows-native-unsupported`); do **not** ship a Windows toast adapter without a smoke-test host. Setup/docs must say: use **`webhook`** on Windows (and any headless host).  
- [ ] Docs: `manual-smoke.md`, `COMPATIBILITY.md`, `plugin/references/README.md`, SECURITY note if needed — cover macOS/Linux desktop vs Windows/SSH/CI.  
- [ ] **Commit** `docs+setup: headless native honesty for notify`

### Task 5.5 — Internal code review (Gate D)

- [ ] Spec + DRY + no auto path regression; direct marker isolation; headless copy accurate.  
- [ ] Artifact `docs/superpowers/reviews/YYYY-MM-DD-pr5-notify-followups.md`.  
- [ ] Zero open remediable findings.  
- [ ] **Commit** `review: PR5 internal review artifact`

### Task 5.6 — Docs + 1.7.0 (Gate E)

- [ ] PR3 = one automatic attempt on hardened durable runs; PR5 = A re-fire + B direct + C headless honesty; none exactly-once.  
- [ ] Packaging **1.7.0**; suites; tag after merge.  
- [ ] **Commit** `release: 1.7.0 notify dogfood follow-ups`

**Not in PR5:** auto-retry loops, delivery guarantees, **Windows native toast implementation** (document + webhook only until a Windows smoke host exists), private-IP webhook denylist (optional later if needed).

---

## Coverage matrix

| Requirement | PR / Task |
|-------------|-----------|
| Seed + recordRevision 0 | PR1 / 1.1 |
| CAS + lock + no full-replace clobber | PR1 / 1.2–1.3 |
| Envelope-first crash-consistent terminal persist | PR1 / 1.3 |
| Worker normal writer; parent recovery only if not alive | PR1 / 1.5 |
| Status read-only; envelope-aware effective lifecycle | PR1 / 1.6 |
| Status exit 1 skill handling | PR1 / 1.6 |
| Monotonic elapsed | PR1 / 1.4 |
| Opt-in review isolation + ownership + `--ita-invisible-in-index` | PR2 (`--isolated` only) |
| Failure-mode matrix + DRY + internal review gates | PR3–PR5 (Gate A/B/D) |
| Execution context + notify attempt; skill-run no-op | PR3 |
| Operator notify re-attempt reuses notify.mjs; no auto-retry | PR5-A |
| Direct-mode job-scoped completion signal | PR5-B |
| Headless/native honesty (setup + docs) | PR5-C |
| Exactly-once notify / guaranteed delivery | **Out of program** |
| Contract + scopes + order (single finalization fn) | PR4 / 4.1–4.3 |
| Operator-trusted validation (no OS FS sandbox claim) | PR4 / 4.5 |
| Single validate_implementation_handoff for write + read | PR4 / 4.7–4.8 |
| terminalOutcome ready + dual-condition handoff | PR4 / 4.7–4.8 |
| Temp-index post-check blocker | PR4 / 4.4 |
| Non-streaming handoff mode | PR4 / 4.8 |
| Factual cleanup warning | PR4 / 4.9 |
| `-z` path tests | PR4 / 4.4 |
| One target workspace | PR4 (constraint) |

---

## Locked decisions checklist

| Topic | Decision |
|-------|----------|
| Status writes | **Never** |
| Effective lifecycle | record / envelope / derived (§6) |
| Terminal persist | Envelope-first; idempotent lifecycle finish (§7.1) |
| Terminal writers | Worker normal; parent durable recovery only if `is_alive() is False` |
| CAS | `recordRevision` + `run.lock` |
| write_run_record public API | Deleted after PR1 migration; CAS only |
| Elapsed | Monotonic in owner process; UTC for status |
| Dirty isolation (when `--isolated`) | `git diff --no-ext-diff --no-textconv --binary --full-index --ita-invisible-in-index <pinned-base-sha> --` |
| Isolation trigger | **`--isolated` only**; `--base` alone = live |
| Notify (PR3) | At-most-once **attempt**; no auto-retry pending; **not** exactly-once |
| Notify follow-ups (PR5) | Operator re-attempt + direct-mode signal + headless honesty; no auto-retry |
| DRY (PR3–PR5) | **No duplicated logic**; shared validators/writers; review fails on copy-paste |
| Internal review (PR3–PR5) | **Required task** + artifact before packaging |
| Background | `GROK_COMPANION_EXECUTION_CONTEXT` via shared skill pattern; skill-run **no-op** |
| Handoff streaming | **No** |
| Contract validation | Operator-trusted; no OS FS sandbox claim |
| Handoff ready | `terminalOutcome` at write; dual-condition at `/grok:handoff` |
| Temp index | Post-check; `temp-index-retained` blocks ready |
| Multi-workspace gate | Out of PR4; one target |
| Releases | Five minors 1.3–1.7 (PR5 optional after dogfood) |
| PROVENANCE.md | No edit in PR1–PR4 |

---

## Execution order

1. PR1 → `v1.3.0`  
2. PR2 → `v1.4.0`  
3. PR3 → `v1.5.0` (matrix → implement → **internal review** → package)  
4. PR4 → `v1.6.0` (same gate sequence)  
5. PR5 → `v1.7.0` (after PR3 dogfood; may trail PR4; same gate sequence)

No parallel tracks for PR1–PR4. PR5 may wait on real notify demand after 1.5.0. No alternate designs during implementation of a given PR.

---

## Handoff

Revision **9:** PR2 isolation opt-in only.  
Revision **10:** PR5 = operator notify re-attempt only.  
Revision **11:** PR3–PR5 **quality gates** (failure-mode matrix, **DRY**, **internal code review as PR tasks**); suites alone are not done.  
Revision **12:** PR5 expanded after PR3 dogfood prioritization: **A** operator re-attempt, **B** direct-mode completion signal, **C** headless/native honesty (1.7.0).

Paths:

- `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- `docs/superpowers/plans/2026-07-15-run-lifecycle.md`  

**PR1 done on main (1.3.x). PR2 in flight. PR3–PR5 per execution order + Gates A–E.**
