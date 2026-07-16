# Run lifecycle program — Implementation Plan (revision 6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Checkboxes track progress.

**Goal:** Full run lifecycle with CAS, read-only status, process finalize worker, isolated review, at-most-once notify attempts, and verified `code` implementation handoff — **four PRs**, zero open decisions.

**Design:** [docs/superpowers/specs/2026-07-15-run-lifecycle-design.md](../specs/2026-07-15-run-lifecycle-design.md) **revision 6**.

**Baseline:** v1.2.10. **Versions:** 1.3.0 → 1.4.0 → 1.5.0 → **1.6.0**.

**Rule:** Do not invent alternatives. Design §4–§14 are authority. Every step below is mandatory as written.

**Do not execute until the user approves revision 6.**

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
| `plugin/wrapper/scripts/groklib/envelope.py` | `finalization-timeout` in ERROR_CLASSES |
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

### PR2 → 1.4.0

| Path | Role |
|------|------|
| `plugin/wrapper/scripts/grok_agent.py` | `--isolated` |
| `plugin/wrapper/scripts/groklib/review_isolation.py` | **New** — prepare + ownership + dirty apply + cleanup |
| `plugin/wrapper/scripts/groklib/modes/review.py` | Call isolation |
| `plugin/wrapper/scripts/groklib/envelope.py` | `isolation-unavailable` |
| `plugin/wrapper/scripts/tests/test_review_isolation.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_review.py` | Wire + concurrent + partial cleanup |
| `plugin/skills/review/SKILL.md` | isolation flags |
| `plugin/skills/adversarial-review/SKILL.md` | isolation if applicable |
| `README.md` | isolation |
| `plugin/references/README.md` | isolation |
| `plugin/wrapper/references/authority-policies.md` | isolation |
| `docs/COMPATIBILITY.md` | |
| `docs/roadmap.md` | 1.4.0 |
| `CHANGELOG.md` | 1.4.0 |
| Packaging triple | **1.4.0** |

**Not in PR2:** `docs/PROVENANCE.md`.

### PR3 → 1.5.0

| Path | Role |
|------|------|
| `plugin/scripts/lib/jobs.mjs` | notificationMode + webhookUrl defaults |
| `plugin/scripts/lib/notify.mjs` | **New** — at-most-once **attempt** |
| `plugin/scripts/grok-companion.mjs` | execution context; notify hooks; never forward context to wrapper |
| `plugin/skills/code/SKILL.md`, `review/SKILL.md`, `reason/SKILL.md`, `adversarial-review/SKILL.md`, `verify/SKILL.md` | Prefix `GROK_COMPANION_EXECUTION_CONTEXT` on companion invocations for wait vs background |
| `plugin/scripts/lib/skill-run.mjs` | Optional env merge helper only if needed; must not default to guessing background |
| Codex agent docs/paths that spawn companion for live modes | Same env signal |
| `plugin/scripts/tests/notify.test.mjs` | **New** |
| `plugin/scripts/tests/jobs.test.mjs` | prefs |
| `plugin/scripts/tests/grok-companion.test.mjs` | context + notify paths |
| `plugin/skills/setup/SKILL.md` | notification flags |
| `README.md` | notify |
| `docs/RELEASE.md` | |
| `plugin/references/manual-smoke.md` | |
| `docs/COMPATIBILITY.md` | |
| `docs/roadmap.md` | 1.5.0 |
| `SECURITY.md` | webhook surface if present |
| `CHANGELOG.md` | 1.5.0 |
| Packaging triple | **1.5.0** |

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
| `plugin/wrapper/scripts/groklib/envelope.py` | six error classes; MODES += `handoff` |
| `plugin/scripts/grok-companion.mjs` | WRAPPER_MODES += `handoff`; **not** STREAMING; `runHandoff()` |
| `plugin/skills/handoff/SKILL.md` | **New** |
| `plugin/skills/handoff/run.mjs` | **New** |
| `plugin/skills/code/SKILL.md` | contract-file; handoff pointer; one target |
| `plugin/wrapper/scripts/tests/test_implementation_contract.py` | **New** |
| `plugin/wrapper/scripts/tests/test_implementation_handoff.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_handoff.py` | **New** |
| `plugin/wrapper/scripts/tests/test_mode_code.py` | order, blockers, ready, validation sandbox |
| `plugin/scripts/tests/grok-companion.test.mjs` | handoff non-streaming / no job |
| Docs: `README.md`, `CHANGELOG.md`, `docs/roadmap.md`, `docs/COMPATIBILITY.md`, `docs/RELEASE.md`, `plugin/references/README.md`, `plugin/references/manual-smoke.md`, `plugin/wrapper/references/authority-policies.md`, `plugin/wrapper/SKILL.md` | all mandatory |
| Packaging triple | **1.6.0** |
| Claude/Codex manifests | packaging triple only (modes discovered from skills dirs; no separate mode list file) |

**Not in PR4:** `docs/PROVENANCE.md`.

---

## PR1 — Lifecycle core

### Task 1.1 — Atomic seed before run-id + caller inventory

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

**Files:** `runstate.py`, tests

- [ ] Implement exclusive `run.lock` (fcntl Unix / msvcrt Windows).  
- [ ] `cas_update_run_record(paths, expected_revision, patch)`.  
- [ ] `set_lifecycle(paths, expected_revision, lifecycle)` with design graph.  
- [ ] Overwrite of terminal lifecycle refuses write.  
- [ ] Concurrent CAS conflict raises / returns conflict (tests).  
- [ ] **Commit** `runstate: CAS recordRevision and run.lock`

### Task 1.3 — Single terminal writer `persist_terminal_envelope`

```python
def persist_terminal_envelope(
    paths: RunPaths,
    expected_revision: int,
    envelope: dict,
    *,
    lifecycle: str,
) -> None:
    # under lock: refuse if valid envelope exists or lifecycle terminal
    # validate envelope; write envelope.json once; CAS lifecycle completed|failed|canceled
```

- [ ] Success: `lifecycle="completed"`.  
- [ ] Failure: `lifecycle="failed"`.  
- [ ] Cancel: `lifecycle="canceled"`.  
- [ ] Test: second terminal write does not replace first.  
- [ ] Test: lifecycle argument never inferred from envelope status alone.  
- [ ] **Commit** `runstate: single terminal envelope writer with CAS`

### Task 1.4 — Progress `elapsedMs` (monotonic owner)

- [ ] ProgressWriter stores `time.monotonic()` start at construction (owning process).  
- [ ] Every emit includes `elapsedMs` and UTC `ts`.  
- [ ] Worker does not write progress.  
- [ ] Parent emits finalizing messages: entering / succeeded / timed out.  
- [ ] Status derives display elapsed from UTC/`createdAtUtc` when needed; clamp negative to 0.  
- [ ] **Commit** `progress: monotonic elapsedMs in owning process`

### Task 1.5 — Finalize worker protocol

**Files:** `modes/finalize_worker.py`, `_shared.py`, `_worktree.py`, `envelope.py`, `tests/test_finalize_watchdog.py`

Implement design §9 exactly:

- Serializable `finalize-payload.json` only.  
- Worker owns auth-home cleanup + envelope build + `persist_terminal_envelope`.  
- Parent owns progress + timeout path after terminate(5s)/kill(5s)/join + re-read under lock.  
- Preserve already-valid terminal envelope on timeout path.  

Tests:

- [ ] Worker completes before timeout → success preserved.  
- [ ] Worker completes during kill window → envelope preserved.  
- [ ] Worker completes after parent would have written timeout → no replacement.  
- [ ] True hang → finalization-timeout once; lifecycle failed.  
- [ ] Spawn payload has no non-serializable fields.  
- [ ] **Commit** `modes: process finalize worker with race-safe terminal write`

### Task 1.6 — Status projection (read-only)

**Files:** `status.py`, `test_mode_status.py`, `plugin/skills/status/SKILL.md`

- [ ] Projection table exactly.  
- [ ] Dead owner + no envelope → **derived** `interrupted` in response only; **no** `set_lifecycle`.  
- [ ] Test: recursive content hash of run dir identical before/after status.  
- [ ] Valid failure envelope → top-level failure, exit 1, envelope relayed.  
- [ ] Skill text: exit 1 can mean inspected failed target; always relay JSON; distinguish parse failure.  
- [ ] **Commit** `status: read-only projection with derived interrupted`

### Task 1.7 — Docs + tag 1.3.0

- [ ] All PR1 docs from file map.  
- [ ] Packaging triple **1.3.0**.  
- [ ] Full Python + Node suites.  
- [ ] Commit + annotated tag `v1.3.0`.

---

## PR2 — Isolated review

### Task 2.1 — Flag

- [ ] `grok_agent.py`: `--isolated` store_true.  
- [ ] **Commit** `cli: add --isolated`

### Task 2.2 — `review_isolation.py`

Implement design §10 exactly:

- Owner marker sibling `{worktree_path}.owner.json`.  
- Never reuse existing path.  
- Dirty: `git diff --binary --full-index HEAD --` from repo root; apply in worktree; reject dirty submodules.  
- Cleanup always: remove worktree, prune, marker, diff.  

- [ ] **Commit** `review: isolation helper with ownership`

### Task 2.3 — Wire review

- [ ] Isolation required for `--base` and/or `--isolated`.  
- [ ] Failure → failure envelope via terminal writer.  
- [ ] finally cleanup always.  
- [ ] **Commit** `review: enforce isolation for --base and --isolated`

### Task 2.4 — Tests

- [ ] worktree add fail → isolation-unavailable.  
- [ ] Tracked dirty appears; untracked does not.  
- [ ] Submodule dirty rejected.  
- [ ] Apply failure → isolation-unavailable.  
- [ ] Concurrent runs; partial cleanup.  
- [ ] Original checkout noise does not force unexpected-edits on isolated review.

### Task 2.5 — Docs + 1.4.0

- [ ] All PR2 docs; packaging triple **1.4.0**; suites; tag `v1.4.0`.

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

- [ ] Skills set `GROK_COMPANION_EXECUTION_CONTEXT=foreground|background`.  
- [ ] Companion never forwards to wrapper argv/env for Grok.  
- [ ] `auto` only background; `native` both; `off` never.  
- [ ] Never on status/handoff/result/jobs/setup alone.  
- [ ] Claude + Codex tests both contexts.  
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

**Create** `implementation_handoff.py` phase 1: temp index uniquely named; finally remove; binary full-index patch; size limit; secret scan; permissions 0600/0700.

- [ ] Tests: add/modify/delete/rename/binary/symlink/mode; untracked in; ignored out; sentinel out; spaces/tabs/newlines/non-ASCII via `-z`.  
- [ ] Apply to base → resultTreeOid.  
- [ ] **Commit** `handoff: phase-1 immutable git patch`

### Task 4.5 — Execute contract validation

**Dedicated task** (design §14.9):

- [ ] Run each requiredValidation after scopes + HEAD.  
- [ ] cwd inside worktree; reject escape.  
- [ ] shell=False; same write confinement as code.  
- [ ] Record evidence before interpreting exit.  
- [ ] Nonzero → blocker; ready false.  
- [ ] Test: validation command cannot write outside worktree.  
- [ ] **Commit** `code: execute sandboxed contract requiredValidation`

### Task 4.6 — Command evidence tails

- [ ] sha256 + 4096 redacted tails + truncated flags.  
- [ ] Never full logs on envelope stdout.  
- [ ] **Commit** `commands: bounded redacted evidence`

### Task 4.7 — Phase-2 handoff + ready after shared gates

- [ ] Final `implementation-handoff.json` only after sandbox, auth-home, build gate, validation, lifecycle resolution.  
- [ ] `validate_implementation_handoff` single function used by writer.  
- [ ] `integration.ready` only per design §14.12.  
- [ ] validation.sources authority fields (§14.10).  
- [ ] Never rewrite ready true after terminal publication.  
- [ ] Tests: cleanup fail, sandbox fail, build fail, validation fail, success ready, multi-blocker list, empty no-changes.  
- [ ] **Commit** `handoff: phase-2 manifest after all gates`

### Task 4.8 — Mode `handoff` (non-streaming)

- [ ] WRAPPER_MODES only; not STREAMING_MODES.  
- [ ] `runHandoff()` like status.  
- [ ] Same validator as writer.  
- [ ] Rehash patch; integrity failure; unavailable.  
- [ ] Skill + run.mjs.  
- [ ] Tests: no Grok process; no companion job; read-only; job id not required.  
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
| Single terminal writer; never replace envelope | PR1 / 1.3, 1.5 |
| Finalize protocol complete | PR1 / 1.5 |
| Status read-only; derived interrupted | PR1 / 1.6 |
| Status exit 1 skill handling | PR1 / 1.6 |
| Monotonic elapsed | PR1 / 1.4 |
| Review ownership + dirty rules | PR2 |
| Execution context + notify attempt | PR3 |
| Contract + scopes + order | PR4 / 4.1–4.3 |
| Execute validation sandboxed | PR4 / 4.5 |
| Two-phase handoff + ready | PR4 / 4.4, 4.7 |
| Non-streaming handoff mode | PR4 / 4.8 |
| Factual cleanup warning | PR4 / 4.9 |
| `-z` path tests | PR4 / 4.4 |
| One target workspace | PR4 (constraint) |

---

## Locked decisions checklist

| Topic | Decision |
|-------|----------|
| Status writes | **Never** |
| Interrupted durable | Not from status; derived display only |
| Terminal writer | Worker; parent timeout only after kill+join+re-read |
| CAS | `recordRevision` + `run.lock` |
| write_run_record public API | Deleted after PR1 migration; CAS only |
| Elapsed | Monotonic in owner process; UTC for status |
| Dirty isolation | `git diff --binary --full-index HEAD --`; no untracked; reject dirty submodules |
| Notify | At-most-once **attempt**; no auto-retry pending |
| Background | `GROK_COMPANION_EXECUTION_CONTEXT` |
| Handoff streaming | **No** |
| Contract validation | Executed sandboxed after scopes/HEAD |
| Handoff ready timing | After all shared gates (phase 2) |
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

Revision **6** incorporates all 24 review findings (4 critical, 9 high, 8 medium, 3 low). Paths:

- `docs/superpowers/specs/2026-07-15-run-lifecycle-design.md`  
- `docs/superpowers/plans/2026-07-15-run-lifecycle.md`  

**Approve revision 6 before any implementation.**
