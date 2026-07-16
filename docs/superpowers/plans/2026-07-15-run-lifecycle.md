# Run lifecycle program — Implementation Plan (revision 9)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Checkboxes track progress.

**Goal:** Full run lifecycle with CAS, crash-consistent terminal persistence, read-only status, process finalize worker, **opt-in** isolated review, at-most-once notify attempts, and verified `code` implementation handoff — **four PRs**.

**Design:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md) **revision 9**.

**Baseline:** v1.2.10; **PR1 shipped** on main as **1.3.x**. **Versions:** 1.3.x (done) → 1.4.0 → 1.5.0 → **1.6.0**.

**Rule:** Design §4–§14 are authority. Rev 9 product change: PR2 isolation is **opt-in** (`--isolated` only); `--base` alone does **not** force isolation.

**PR2 may proceed under revision 9 after user approval of this opt-in policy.**

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
| Packaging triple | **1.5.0** |

**Not in PR3:** changes to `plugin/scripts/lib/skill-run.mjs` behavior (locked no-op).

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

- [ ] `grok_agent.py`: `--isolated` store_true, **default false**.  
- [ ] **Commit** `cli: add opt-in --isolated`

### Task 2.2 — `review_isolation.py`

Implement design §10 exactly (only used when isolation is requested):

- Owner marker sibling `{worktree_path}.owner.json`.  
- Never reuse existing path.  
- Dirty: `git diff --binary --full-index --ita-invisible-in-index HEAD --` from repo root; apply in worktree; reject dirty submodules.  
- Cleanup always: remove worktree, prune, marker, diff.  

- [ ] **Commit** `review: isolation helper with ownership`

### Task 2.3 — Wire review

- [ ] Call isolation helper **iff** `args.isolated` (or equivalent) is true.  
- [ ] `--base` without `--isolated` → **no** isolation path.  
- [ ] Isolation failure → failure envelope via terminal writer (`isolation-unavailable`).  
- [ ] finally cleanup always when isolation was started.  
- [ ] **Commit** `review: opt-in isolation via --isolated only`

### Task 2.4 — Tests

- [ ] Without `--isolated`: live review path unchanged (including with `--base`).  
- [ ] With `--isolated`: worktree add fail → isolation-unavailable.  
- [ ] Tracked dirty (staged+unstaged) appears; untracked does not.  
- [ ] `git add -N` intent-to-add does **not** appear in isolated tree.  
- [ ] Submodule dirty rejected.  
- [ ] Apply failure → isolation-unavailable (no live fallback).  
- [ ] Concurrent isolated runs; partial cleanup.  
- [ ] Isolated run: original checkout noise does not force unexpected-edits.

### Task 2.5 — Docs + 1.4.0

- [ ] All PR2 docs; packaging triple **1.4.0**; suites; tag `v1.4.0`.  
- [ ] Explicit docs: opt-in only; when to use `--isolated` vs live `--base`.

---

## PR3 — Notifications

### Task 3.1 — Jobs config

- [ ] Defaults `notificationMode: "off"`, `notificationWebhookUrl: null`.  
- [ ] **Commit** `jobs: notification prefs`

### Task 3.2 — `notify.mjs` at-most-once attempt

Design §11:

- Exclusive create `pending` before external call.  
- Existing marker → skip `already-attempted` (no auto-retry).  
- Always complete marker with result; never leave intentional retry loop.  
- Prioritize no duplicate attempts over guaranteed delivery.  
- Do not document as exactly-once.  

- [ ] Tests: off; already-attempted; crash-left pending not auto-retried; spawn never shell true.  
- [ ] **Commit** `notify: at-most-once attempt semantics`

### Task 3.3 — Companion execution context + hooks

- [ ] Update each PR3 skill/agent path in the file map to prefix `GROK_COMPANION_EXECUTION_CONTEXT=foreground|background` on wait vs background.  
- [ ] **Do not** change `skill-run.mjs` behavior.  
- [ ] Companion never forwards context to wrapper.  
- [ ] `auto` only background; `native` both; `off` never.  
- [ ] Never on status/handoff/result/jobs/setup alone.  
- [ ] Tests: foreground + background for Claude skill path and Codex/agent path.  
- [ ] **Commit** `companion: execution context and notify hooks`

### Task 3.4 — Docs + 1.5.0

- [ ] All PR3 docs; packaging **1.5.0**; tag `v1.5.0`.

---

## PR4 — Verified implementation handoff (→ 1.6.0)

### Task 4.1 — Contract module

**Create** `implementation_contract.py` per design §14.3.

- [ ] Parse/validate schemaVersion, taskId, target, scopes, requiredValidation argv.  
- [ ] `path_in_scopes` component semantics.  
- [ ] Classify `implementation-contract-invalid`.  
- [ ] Document trust model in module docstring.  
- [ ] Tests: prefix confusion, traversal, absolute, empty scopes.  
- [ ] **Commit** `contract: parse write scopes and validation descriptors`

### Task 4.2 — Unexpected commit as blocker

- [ ] After Grok: HEAD must equal base; else append blocker `unexpected-commit`; no reset; continue if readable.  
- [ ] **Commit** `code: unexpected-commit blocker without aborting forensics`

### Task 4.3 — Finalization order + write scopes

Implement design §14.6 order exactly:

```text
verify sentinel → remove exact sentinel → HEAD check → changed files
→ write scopes → forensic patch → requiredValidation → build gate
→ shared safety → ready → final handoff JSON → terminal envelope
```

- [ ] Sentinel never in changed files/patch.  
- [ ] Malformed/missing/symlink sentinel fails.  
- [ ] Cannot remove user-authored similarly named path.  
- [ ] Scope violation → blocker + continue forensics when safe.  
- [ ] **Commit** `code: locked post-Grok finalization order`

### Task 4.4 — Phase-1 forensic patch

**Create** `implementation_handoff.py` phase 1: temp index uniquely named; `finally` delete + post-check per design §14.7; binary full-index patch; size limit; secret scan; permissions 0600/0700.

- [ ] If index still exists after cleanup → blocker `temp-index-retained`, ready false.  
- [ ] If delete errors but path absent → warning only.  
- [ ] Tests: add/modify/delete/rename/binary/symlink/mode; untracked in; ignored out; sentinel out; spaces/tabs/newlines/non-ASCII via `-z`; both cleanup cases.  
- [ ] Apply to base → resultTreeOid.  
- [ ] **Commit** `handoff: phase-1 immutable git patch`

### Task 4.5 — Execute contract validation (operator-trusted)

**Dedicated task** (design §14.3, §14.9):

- [ ] Run each requiredValidation after scopes + HEAD.  
- [ ] cwd inside worktree; reject escape.  
- [ ] shell=False; **no OS FS sandbox claim**.  
- [ ] Post-command original-checkout unmodified assertion.  
- [ ] Record evidence before interpreting exit.  
- [ ] Nonzero → blocker; ready false.  
- [ ] trustModel string `operator-contract-trusted-no-os-sandbox`.  
- [ ] Tests: cwd escape rejected; shell not used; original-checkout dirty after validation blocks ready.  
- [ ] **Do not** test or document “cannot write outside worktree” as OS guarantee.  
- [ ] **Commit** `code: execute operator-trusted contract requiredValidation`

### Task 4.6 — Command evidence tails

- [ ] sha256 + 4096 redacted tails + truncated flags.  
- [ ] Never full logs on envelope stdout.  
- [ ] **Commit** `commands: bounded redacted evidence`

### Task 4.7 — Phase-2 handoff + ready from terminalOutcome

- [ ] After gates: decide in-memory `terminalOutcome`; compute ready per §14.12 (not disk lifecycle).  
- [ ] Write final `implementation-handoff.json` **before** `persist_terminal_envelope`.  
- [ ] Then envelope-first terminal persist.  
- [ ] `validate_implementation_handoff` single function used by writer.  
- [ ] validation.sources authority fields (§14.10).  
- [ ] Never rewrite ready-true manifest after terminal envelope published.  
- [ ] Tests: sandbox/build/validation fail; success ready; multi-blocker; empty no-changes; crash between manifest and envelope; crash between envelope and lifecycle.  
- [ ] **Commit** `handoff: phase-2 manifest from terminalOutcome`

### Task 4.8 — Mode `handoff` (non-streaming)

- [ ] WRAPPER_MODES only; not STREAMING_MODES.  
- [ ] `runHandoff()` like status.  
- [ ] Same validator as writer.  
- [ ] Observed ready requires manifest ready **and** completed terminal envelope (§14.12).  
- [ ] Rehash patch; integrity failure; unavailable; `terminal-envelope-incomplete` when manifest ready but envelope missing.  
- [ ] Skill + run.mjs.  
- [ ] Tests: no Grok process; no companion job; read-only; job id not required; dual-condition ready.  
- [ ] **Commit** `handoff: read-only non-streaming /grok:handoff`

### Task 4.9 — Cleanup factual warning

Exact meaning design §14.17 — no “unacknowledged.”

- [ ] **Commit** `cleanup: warn on integration-ready handoff removal`

### Task 4.10 — Docs + dual-host smoke + 1.6.0

- [ ] All PR4 docs from file map.  
- [ ] Parent protocol + parallel rules + transfer vs handoff.  
- [ ] Path headers / skill frontmatter on all new files.  
- [ ] Suites + `claude plugin validate ./plugin --strict`.  
- [ ] Dual-host smoke design §14.19.  
- [ ] Packaging triple **1.6.0**; tag `v1.6.0`.

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
| Execution context + notify attempt; skill-run no-op | PR3 |
| Contract + scopes + order | PR4 / 4.1–4.3 |
| Operator-trusted validation (no OS FS sandbox claim) | PR4 / 4.5 |
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
| Dirty isolation (when `--isolated`) | `git diff --binary --full-index --ita-invisible-in-index HEAD --` |
| Isolation trigger | **`--isolated` only**; `--base` alone = live |
| Notify | At-most-once **attempt**; no auto-retry pending |
| Background | `GROK_COMPANION_EXECUTION_CONTEXT` via skill/agent SKILL/md; skill-run **no-op** |
| Handoff streaming | **No** |
| Contract validation | Operator-trusted; no OS FS sandbox claim |
| Handoff ready | `terminalOutcome` at write; dual-condition at `/grok:handoff` |
| Temp index | Post-check; `temp-index-retained` blocks ready |
| Multi-workspace gate | Out of PR4; one target |
| Releases | Four minors 1.3–1.6 |
| PROVENANCE.md | No edit in PR1–PR4 |

---

## Execution order

1. PR1 → `v1.3.0`  
2. PR2 → `v1.4.0`  
3. PR3 → `v1.5.0`  
4. PR4 → `v1.6.0`  

No parallel tracks. No alternate designs during implementation.

---

## Handoff

Revision **9** product change after PR1 ship: **PR2 isolation is opt-in** (`--isolated` only). `--base` no longer requires isolation. Paths:

- `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- `docs/superpowers/plans/2026-07-15-run-lifecycle.md`  

**PR1 is done on main (1.3.x). Approve revision 9 before implementing PR2.**
