<!-- docs/superpowers/plans/2026-07-16-peer-agent-integration.md -->

# Peer-Agent Integration Implementation Plan (grok-skills 2.0.0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship grok-skills **2.0.0**: Grok as a true peer implementer agent inside Claude Code and Codex - multi-turn iteration on a run, acceptance criteria that flow end to end, a one-call delegate flow, direct-mode parity honesty, native 2026 host surfaces (bin/, plugin data dir, new hooks, userConfig), Codex parity polish, manifest polish with a dual-manifest drift guard, and the ACP peer channel (probe-gated experimental implementation).

**Architecture:** Everything builds on the existing companion -> hardened wrapper -> single-envelope pipeline. No new runtime deps. The iteration loop (Phase 2) archives the private-home Grok session store into the run dir before teardown and reseeds it on `--continue-run`, reusing the retained worktree. Host-surface work (Phases 3-4) is additive and never removes the `run.mjs` path (Codex has no `bin/` support).

**Tech Stack:** Python 3 stdlib (wrapper), Node stdlib (companion/hooks), Claude Code plugin manifest v2026, Codex plugin TOML agents.

## Global Constraints (from AGENTS.md - every task inherits these)

- Stdlib only: no pip/npm runtime packages.
- One stdout envelope per wrapper run; progress/relay -> stderr only. Companion combo modes (existing precedent: `debate`) may relay multiple wrapper envelopes sequentially.
- 900-line file cap; path-header comment on every code file.
- ASCII hyphens only in prose/comments/commits.
- Fail closed on unverifiable state; never fail closed on Grok CLI build string.
- No secrets in source; test fixtures split secret-shaped literals.
- No personal/monorepo paths in committed code or docs.
- Docs follow code: every task's final step updates README.md, CHANGELOG.md, relevant `docs/**`, skill SKILL.mds, `plugin/references/**`, and `docs/roadmap.md` status.
- Dual-host parity: Claude + Codex manifests/skills stay aligned; when a feature is host-specific, document the asymmetry explicitly.
- Wrapper tests: `cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q` (653 pass today). Plugin tests: `cd plugin/scripts && node --test tests/*.test.mjs` (172 pass today). Both suites must stay green after every task.
- Version bumps + releases only via docs/RELEASE.md.

## Release strategy: ONE release, 2.0.0

Everything in this plan ships together as **2.0.0**. No intermediate public versions:

- Work merges phase-by-phase as sequential PRs (PR6..PR11 below) into a long-lived `feat/2.0-peer-agent` integration branch (or straight to main if you prefer trunk - either way, no tag until Phase 6).
- **No version bumps inside Phases 0-5.** Both `plugin.json` files stay at their current version until Phase 6 (the release phase) bumps every packaging surface to `2.0.0` in one commit, per docs/RELEASE.md.
- Every user-facing doc string added by Phases 0-5 that names a version writes `2.0.0+` (never an intermediate number).
- Each phase ends with a **docs checkpoint** task (README/CHANGELOG-draft/skills/references/roadmap kept in lockstep with the code - Non-negotiable #1) but CHANGELOG entries accumulate under one `## 2.0.0 (unreleased)` heading until Phase 6 finalizes it.

| PR | Phase | Content |
|----|-------|---------|
| PR6 | 0 | Hygiene (900-line cap, DRY task staging, argv-safety reference, divergence warning) |
| PR7 | 1 | Acceptance criteria wired + `implement` combo + unified IDs + direct-mode parity |
| PR8 | 2 | Iteration loop: session archive + `code --continue-run` |
| PR9 | 3 | Claude Code native surface (bin/, data dir, SubagentStop hook, userConfig, agent frontmatter) |
| PR10 | 4 | Codex parity polish |
| PR11 | 5+6 | ACP probe + spec + experimental peer channel; manifest polish; release 2.0.0 |

(PR5 / notify dogfood follow-ups from the existing roadmap are independent; if unshipped when 2.0.0 cuts, fold them in or explicitly re-slate them - decide at Phase 6, do not silently drop.)

Already shipped, do NOT re-plan: native `--output-format streaming-json` + `--json-schema` (grokcli.py `build_argv`), repo-rules prompt injection (rules.py), dual-condition handoff (1.6.0).

---

# Phase 0 - Hygiene (PR6)

### Task 0.1: Split envelope.py under the 900-line cap

**Files:**
- Create: `plugin/wrapper/scripts/groklib/redaction.py`
- Modify: `plugin/wrapper/scripts/groklib/envelope.py` (984 lines today)
- Test: existing `plugin/wrapper/scripts/tests/test_envelope.py` (no behavior change; imports keep resolving)

**Interfaces:**
- Consumes: current `envelope.py` internals.
- Produces: `groklib.redaction` module owning the secret-pattern table and the pure redaction functions: `SECRET_PATTERNS`, `redact_secret_value_text(text: str) -> str`, `redact_secret_material(value, redact_keys: bool = False)`, `SecretMaterialError`, `assert_no_secret_material(obj) -> None`. `envelope.py` re-exports all five names verbatim (`from groklib.redaction import ...`) so every existing import site (`grok_agent.py`, `implementation_handoff.py`, tests) keeps working unchanged.

- [ ] **Step 1: Locate the redaction block**

Run: `rg -n "SECRET_PATTERNS|def redact_secret_value_text|def redact_secret_material|class SecretMaterialError|def assert_no_secret_material" plugin/wrapper/scripts/groklib/envelope.py`
Record the line ranges; the move must be cut-paste exact (no logic edits).

- [ ] **Step 2: Create `groklib/redaction.py`**

Move the pattern table + the four symbols listed above into the new file with this header:

```python
# wrapper/scripts/groklib/redaction.py
#
# Secret-material pattern table and redaction primitives (single source; C4
# envelope scanning and handoff blocker redaction both import from here).
# Extracted from envelope.py for the 900-line cap. The Node progress relay
# mirrors SECRET_PATTERNS (plugin/scripts/progress-relay.mjs) under a drift
# test - update both together.
```

Bring along only the imports those functions need (`re`, `json` if used).

- [ ] **Step 3: Re-export from envelope.py**

At the top of `envelope.py`, replace the moved block with:

```python
# Redaction primitives moved to groklib.redaction (900-line cap). Re-exported
# so every existing "from groklib.envelope import redact_*" site keeps working.
from groklib.redaction import (  # noqa: F401
    SECRET_PATTERNS,
    SecretMaterialError,
    assert_no_secret_material,
    redact_secret_material,
    redact_secret_value_text,
)
```

- [ ] **Step 4: Verify the cap and the suite**

Run: `wc -l plugin/wrapper/scripts/groklib/envelope.py plugin/wrapper/scripts/groklib/redaction.py`
Expected: both < 900.
Run: `cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q`
Expected: `OK` (653 tests).
Run: `cd plugin/scripts && node --test tests/*.test.mjs` (the Node drift test that mirrors SECRET_PATTERNS must still find them - if it greps envelope.py by path, update the drift test to read `groklib/redaction.py`).
Expected: 172 pass.

- [ ] **Step 5: Commit**

```bash
git add plugin/wrapper/scripts/groklib/redaction.py plugin/wrapper/scripts/groklib/envelope.py plugin/scripts/tests
git commit -m "refactor: extract groklib.redaction from envelope.py (900-line cap)"
```

### Task 0.2: Dedupe task-file staging in the companion

**Files:**
- Create: `plugin/scripts/lib/task-file.mjs`
- Modify: `plugin/scripts/grok-companion.mjs:90-109` (`stageStdinTaskFile`) and `:180-203` (`injectTaskFile`)
- Test: `plugin/scripts/tests/task-file.test.mjs`

**Interfaces:**
- Produces: `stageTaskFile(taskText: string) -> { taskPath: string, cleanup: () => void }` - single owner of the mkdtemp + 0600 write + rm cleanup. `stageStdinTaskFile(args)` and `injectTaskFile(args, taskText)` keep their exact current signatures and behavior but both call `stageTaskFile` internally; both are exported from the new module and re-imported by `grok-companion.mjs`.

- [ ] **Step 1: Write the failing test**

```js
// plugin/scripts/tests/task-file.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { stageTaskFile, injectTaskFile } from "../lib/task-file.mjs";

test("stageTaskFile writes 0600 file and cleanup removes it", () => {
  const { taskPath, cleanup } = stageTaskFile("hello task");
  assert.equal(fs.readFileSync(taskPath, "utf8"), "hello task");
  const mode = fs.statSync(taskPath).mode & 0o777;
  assert.equal(mode, 0o600);
  cleanup();
  assert.equal(fs.existsSync(taskPath), false);
});

test("injectTaskFile strips old task flags and appends staged file", () => {
  const { args, cleanup } = injectTaskFile(["code", "--task", "old", "--target", "."], "new text");
  assert.ok(!args.includes("--task") || args[args.indexOf("--task-file") + 1]);
  assert.equal(args[0], "code");
  const tf = args.indexOf("--task-file");
  assert.ok(tf > 0);
  assert.equal(fs.readFileSync(args[tf + 1], "utf8"), "new text");
  cleanup();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd plugin/scripts && node --test tests/task-file.test.mjs`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `lib/task-file.mjs`**

```js
// plugin/scripts/lib/task-file.mjs
//
// Single owner of task-text temp staging (mkdtemp + 0600 + cleanup). Both the
// stdin --task-file - path and companion task injection go through here (DRY).
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { readAllStdinSync } from "./read-stdin.mjs";

export function stageTaskFile(taskText) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-task-"));
  const taskPath = path.join(dir, "task");
  fs.writeFileSync(taskPath, taskText, { mode: 0o600 });
  return {
    taskPath,
    cleanup: () => {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        // best-effort temp cleanup
      }
    },
  };
}

export function stageStdinTaskFile(args) {
  const flagIndex = args.indexOf("--task-file");
  if (flagIndex < 0 || args[flagIndex + 1] !== "-") {
    return null;
  }
  const { taskPath, cleanup } = stageTaskFile(readAllStdinSync());
  const staged = args.slice();
  staged[flagIndex + 1] = taskPath;
  return { args: staged, cleanup };
}

export function injectTaskFile(args, taskText) {
  const cleaned = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--task" || args[i] === "--task-file") {
      i += 1;
      continue;
    }
    cleaned.push(args[i]);
  }
  const { taskPath, cleanup } = stageTaskFile(taskText);
  cleaned.push("--task-file", taskPath);
  return { args: cleaned, cleanup };
}
```

Note: `readAllStdinSync` returns bytes today; `stageTaskFile` accepts string or Buffer (`fs.writeFileSync` handles both) - keep passing the bytes through unchanged.

- [ ] **Step 4: Rewire grok-companion.mjs**

Delete the two local function definitions; add `import { stageStdinTaskFile, injectTaskFile } from "./lib/task-file.mjs";`. The `stderrLine` diagnostics from the old `stageStdinTaskFile` cleanup move into the shared cleanup (silent best-effort is acceptable; keep one stderr note on failure if you prefer parity - either way, identical for both callers).

- [ ] **Step 5: Run both suites, commit**

Run: `cd plugin/scripts && node --test tests/*.test.mjs`
Expected: all pass (172 + 2 new).

```bash
git add plugin/scripts/lib/task-file.mjs plugin/scripts/grok-companion.mjs plugin/scripts/tests/task-file.test.mjs
git commit -m "refactor: dedupe task-file staging into lib/task-file.mjs"
```

### Task 0.3: Extract the argv-safety prose to one reference

**Files:**
- Create: `plugin/references/argv-safety.md`
- Modify: `plugin/skills/code/SKILL.md:52-67`, `plugin/skills/review/SKILL.md` (same block), `plugin/skills/verify/SKILL.md` (same block), plus any other SKILL.md carrying the copy-pasted quoting paragraphs (`rg -l "single-quoted heredoc" plugin/skills`)

**Interfaces:**
- Produces: one canonical injection-safety document. Skills keep a 3-line summary + link (skills are read by the host model at invocation time, so the load-bearing rule - "single-quote every substituted value; task text only via `--task-file -` heredoc" - stays inline; the rationale moves to the reference).

- [ ] **Step 1: Write `plugin/references/argv-safety.md`**

Move the full explanation (both the `--task` STDIN rule and the flag-VALUE single-quoting rule with the "command-substituted locally BEFORE the wrapper" rationale) verbatim from `plugin/skills/code/SKILL.md:52-67` into the new file, under headings `## Task text` and `## Flag values`.

- [ ] **Step 2: Replace the block in each SKILL.md**

In each skill, replace the two long bullets with:

```markdown
- Injection safety (canonical: `plugin/references/argv-safety.md`): task text is
  NEVER placed in a shell-evaluated position - deliver it with `--task-file -`
  and a SINGLE-QUOTED heredoc. Every substituted flag VALUE is wrapped in
  single quotes (`--target '<path>'`). Bare flags (`--web`) carry no value.
```

- [ ] **Step 3: Verify no drift and commit**

Run: `rg -c "command-substituted locally" plugin/skills plugin/references`
Expected: 1 hit total (the reference).
Run: `claude plugin validate ./plugin --strict` (skills changed).

```bash
git add plugin/references/argv-safety.md plugin/skills
git commit -m "docs: extract argv injection-safety prose to one reference (DRY)"
```

### Task 0.4: Warn when AGENTS.md and CLAUDE.md diverge

**Files:**
- Modify: `plugin/wrapper/scripts/groklib/rules.py` (representative-file selection near line 218)
- Test: `plugin/wrapper/scripts/tests/test_rules.py`

**Interfaces:**
- Consumes: `discover_instruction_files(repo_root, target_abs, require_parity=...)` and its returned instruction objects.
- Produces: `discover_instruction_files` gains a `warnings: List[str]` output channel. Concretely: it returns the same structure as today plus a new `divergence_warnings` attribute/list (choose the shape that matches the existing return type - if it returns a list of entries, return `(entries, warnings)` and update the two call sites: `modes/code.py:581` and wherever review/reason call it; `rg -n "discover_instruction_files" plugin/wrapper/scripts` lists all call sites). Each warning string: `"AGENTS.md and CLAUDE.md differ at <dir>; only AGENTS.md was sent to Grok (set ruleFileParity to enforce matching pairs)"`. Callers append these to the run's `warnings` list so they surface in the envelope.

- [ ] **Step 1: Write the failing test**

```python
# in plugin/wrapper/scripts/tests/test_rules.py
def test_divergent_agents_and_claude_md_yields_warning(self):
    root = pathlib.Path(self.make_temp_repo())  # reuse the module's existing repo fixture helper
    (root / "AGENTS.md").write_text("agents rules\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("different claude rules\n", encoding="utf-8")
    entries, warnings = rules.discover_instruction_files(root, root, require_parity=False)
    self.assertTrue(any("only AGENTS.md was sent" in w for w in warnings))

def test_identical_pair_yields_no_warning(self):
    root = pathlib.Path(self.make_temp_repo())
    (root / "AGENTS.md").write_text("same rules\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("same rules\n", encoding="utf-8")
    entries, warnings = rules.discover_instruction_files(root, root, require_parity=False)
    self.assertEqual(warnings, [])
```

Adapt fixture helper names to what `test_rules.py` already uses (read the file first; reuse its tempdir/git-init helpers).

- [ ] **Step 2: Run to verify failure**

Run: `cd plugin/wrapper/scripts && python3 -m unittest tests.test_rules -q`
Expected: FAIL (return arity / missing warning).

- [ ] **Step 3: Implement**

In `rules.py`, at the point where AGENTS.md is chosen as representative when both exist (line ~218): read both files; when contents differ byte-for-byte, append the warning string. Thread `warnings` through the return. Update all call sites (`modes/code.py` `_prepare`, review/reason equivalents) to unpack and extend the mode's `warnings` list. In `code.py` `_prepare` there is no warnings list in scope - `WorktreePrep` has no warnings field; simplest correct route: `stage.acc.warnings.extend(rule_warnings)` (the `WorktreeStage.acc` accumulator already carries `warnings`; confirm via `rg -n "acc.warnings" plugin/wrapper/scripts/groklib/modes`).

- [ ] **Step 4: Run the full wrapper suite**

Run: `cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q`
Expected: OK. Fix any other `discover_instruction_files` unpack sites the suite flags.

- [ ] **Step 5: Commit**

```bash
git add plugin/wrapper/scripts/groklib/rules.py plugin/wrapper/scripts/groklib/modes plugin/wrapper/scripts/tests/test_rules.py
git commit -m "feat: surface AGENTS.md/CLAUDE.md divergence as an envelope warning"
```

### Task 0.5: Phase 0 docs checkpoint (no version bump)

- [ ] Start the `## 2.0.0 (unreleased)` CHANGELOG.md section (redaction split, task-file DRY, argv-safety reference, divergence warning). Add a README troubleshooting row for the divergence warning ("AGENTS.md/CLAUDE.md differ" -> informational; set `ruleFileParity: true` in `.grok-skills.json` to enforce pairs). No version bump (Phase 6 owns it).

```bash
git add CHANGELOG.md README.md
git commit -m "docs: phase 0 hygiene checkpoint (2.0.0 unreleased notes)"
```

---

# Phase 1 - Acceptance criteria + one-call implement + unified IDs + direct-mode parity (PR7)

### Task 1.1: Inject contract objective + acceptanceCriteria into Grok's prompt

**Files:**
- Modify: `plugin/wrapper/scripts/groklib/modes/code.py` (add `_contract_directive`, call it in `_prepare` at line ~586)
- Test: `plugin/wrapper/scripts/tests/test_mode_code.py`

**Interfaces:**
- Consumes: the normalized contract dict from `implementation_contract.validate_contract` (fields: `taskId`, `objective: str`, `acceptanceCriteria: list`, `writeScopes`, `requiredValidation`).
- Produces: `_contract_directive(contract: Optional[dict]) -> str` - returns `""` for `None`. The prompt composition in `_prepare` becomes: `task_with_sentinel = _sentinel_directive(...) + _contract_directive(contract) + task_text`.

- [ ] **Step 1: Write the failing test**

```python
# in plugin/wrapper/scripts/tests/test_mode_code.py
def test_contract_directive_includes_objective_criteria_and_scopes(self):
    contract = {
        "schemaVersion": 1,
        "taskId": "T-1",
        "objective": "Fix the paginator off-by-one",
        "target": ".",
        "writeScopes": [{"kind": "subtree", "path": "src/pager"}],
        "acceptanceCriteria": [
            "page 2 of a 21-item list shows item 11 first",
            "existing pager tests still pass",
        ],
        "requiredValidation": [{"argv": ["true"], "cwd": ".", "purpose": "smoke"}],
        "trustModel": "operator-contract-trusted-no-os-sandbox",
    }
    text = code_mode._contract_directive(contract)
    self.assertIn("Fix the paginator off-by-one", text)
    self.assertIn("page 2 of a 21-item list", text)
    self.assertIn("src/pager", text)
    self.assertIn("only within these paths", text.lower())

def test_contract_directive_empty_without_contract(self):
    self.assertEqual(code_mode._contract_directive(None), "")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd plugin/wrapper/scripts && python3 -m unittest tests.test_mode_code -q`
Expected: FAIL (`AttributeError: _contract_directive`).

- [ ] **Step 3: Implement `_contract_directive`**

```python
def _contract_directive(contract: Optional[dict]) -> str:
    """Render the operator contract as prompt text so Grok knows the objective,
    the acceptance criteria it must satisfy, and the write scopes it must stay
    inside. Enforcement stays wrapper-side (scopes/validation at finalize);
    this directive is steering, not the enforcement surface."""
    if not contract:
        return ""
    lines: List[str] = ["## Implementation contract (operator-supplied)", ""]
    objective = contract.get("objective") or ""
    if objective.strip():
        lines += ["Objective: {}".format(objective.strip()), ""]
    criteria = [c for c in contract.get("acceptanceCriteria") or [] if isinstance(c, str) and c.strip()]
    if criteria:
        lines.append("Acceptance criteria (ALL must hold when you finish):")
        lines += ["- {}".format(c.strip()) for c in criteria]
        lines.append("")
    scopes = contract.get("writeScopes") or []
    if scopes:
        lines.append("You may create or modify files ONLY within these paths (relative to the repo root):")
        lines += ["- {} ({})".format(s.get("path"), s.get("kind")) for s in scopes]
        lines.append("Changes outside these scopes will be rejected by the wrapper.")
        lines.append("")
    required = contract.get("requiredValidation") or []
    if required:
        lines.append("After implementing, these commands must exit 0 (the wrapper runs them):")
        lines += ["- {}".format(" ".join(e.get("argv", []))) for e in required]
        lines.append("")
    return "\n".join(lines) + "\n"
```

Wire into `_prepare` (code.py line ~586):

```python
task_with_sentinel = _sentinel_directive(sentinel_name) + _contract_directive(contract) + task_text
```

- [ ] **Step 4: Run the suite**

Run: `cd plugin/wrapper/scripts && python3 -m unittest tests.test_mode_code -q` -> pass; then full discover -> OK.

- [ ] **Step 5: Commit**

```bash
git add plugin/wrapper/scripts/groklib/modes/code.py plugin/wrapper/scripts/tests/test_mode_code.py
git commit -m "feat: inject contract objective/acceptanceCriteria/scopes into code prompt"
```

### Task 1.2: Echo the contract summary in the handoff manifest and handoff response

**Files:**
- Modify: `plugin/wrapper/scripts/groklib/code_handoff_finalize.py` (manifest assembly), `plugin/wrapper/scripts/groklib/implementation_handoff.py` (`validate_implementation_handoff` stays permissive: extra keys already allowed - add optional-shape validation only), `plugin/wrapper/scripts/groklib/modes/handoff.py` (response echo)
- Test: `plugin/wrapper/scripts/tests/test_implementation_handoff.py`, `plugin/wrapper/scripts/tests/test_mode_handoff.py`

**Interfaces:**
- Produces: manifest gains optional `contractSummary` (null when no contract): `{"taskId": str, "objective": str, "acceptanceCriteria": [str]}` - display metadata for the parent, NOT part of readiness. `validate_implementation_handoff` accepts it when present with those types, rejects wrong types (fail closed on corrupt manifests). `/grok:handoff` response includes `response.contractSummary` verbatim from the manifest.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_implementation_handoff.py
def test_contract_summary_optional_and_typed(self):
    doc = self.make_valid_manifest()          # reuse the file's existing valid-manifest fixture
    self.assertEqual(validate_implementation_handoff(doc), [])
    doc["contractSummary"] = {"taskId": "T-1", "objective": "x", "acceptanceCriteria": ["a"]}
    self.assertEqual(validate_implementation_handoff(doc), [])
    doc["contractSummary"] = {"taskId": 5}
    self.assertTrue(any("contractSummary" in e for e in validate_implementation_handoff(doc)))
```

```python
# tests/test_mode_handoff.py
def test_handoff_response_echoes_contract_summary(self):
    # extend the existing ready-manifest fixture flow: write a manifest that
    # includes contractSummary, run handoff mode, assert the summary appears
    # under envelope["response"]["contractSummary"].
```

(Write the second test against the file's existing handoff-mode harness; it already builds manifest+envelope fixtures for dual-condition tests - copy the nearest ready-path test and add the field.)

- [ ] **Step 2: Run to verify failure**

Run: `cd plugin/wrapper/scripts && python3 -m unittest tests.test_implementation_handoff tests.test_mode_handoff -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `validate_implementation_handoff` (implementation_handoff.py, after the `worktree` check ~line 227):

```python
    summary = doc.get("contractSummary")
    if summary is not None:
        if not isinstance(summary, dict):
            errors.append("contractSummary must be object or null")
        else:
            if not isinstance(summary.get("taskId"), str):
                errors.append("contractSummary.taskId must be string")
            if not isinstance(summary.get("objective"), str):
                errors.append("contractSummary.objective must be string")
            ac = summary.get("acceptanceCriteria")
            if not isinstance(ac, list) or not all(isinstance(c, str) for c in ac):
                errors.append("contractSummary.acceptanceCriteria must be string array")
```

In `code_handoff_finalize.py`, where the manifest dict is assembled (find via `rg -n "contractSha256" plugin/wrapper/scripts/groklib/code_handoff_finalize.py`), add:

```python
        "contractSummary": (
            {
                "taskId": contract.get("taskId"),
                "objective": contract.get("objective") or "",
                "acceptanceCriteria": list(contract.get("acceptanceCriteria") or []),
            }
            if contract
            else None
        ),
```

In `modes/handoff.py`, where the success response dict is built from the manifest, add `"contractSummary": manifest.get("contractSummary")`.

- [ ] **Step 4: Full wrapper suite**

Run: `cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q` -> OK.

- [ ] **Step 5: Update docs and commit**

Update `plugin/references/implementation-handoff.md` manifest-fields table (add `contractSummary`), `plugin/skills/handoff/SKILL.md` (mention the parent should check criteria against the summary before applying).

```bash
git add plugin/wrapper/scripts plugin/references/implementation-handoff.md plugin/skills/handoff/SKILL.md
git commit -m "feat: carry contract summary (objective + acceptance criteria) through handoff"
```

### Task 1.3: Agents author a contract by default

**Files:**
- Modify: `plugin/agents/grok-engineer-coder.md`, `plugin/codex-agents/grok-engineer-coder.toml`, `plugin/skills/code/SKILL.md`
- Test: `plugin/scripts/tests/codex-agents.test.mjs` (template materialization already tested; assert the new instruction text survives materialization)

**Interfaces:**
- Produces: instruction text telling the host agent to derive a contract JSON from the user's ask before calling `code`, staged to a temp file, passed via `--contract-file`. This is prompt/docs work - no code change - but it is what makes Task 1.1/1.2 actually fire in practice.

- [ ] **Step 1: Add to `plugin/agents/grok-engineer-coder.md` (before the "Implementation call" section)**

```markdown
## Derive a contract (default; skip only for exploratory tasks)

Before calling `code`, derive an implementation contract from the user's ask
and write it to a temp file (hardened mode only; direct mode rejects it):

```bash
CONTRACT_FILE="$(mktemp -t grok-contract)"
cat > "$CONTRACT_FILE" <<'GROK_CONTRACT'
{
  "schemaVersion": 1,
  "taskId": "<short-slug-from-the-ask>",
  "target": "<same value as --target>",
  "objective": "<one-sentence goal in the user's words>",
  "writeScopes": [{"kind": "subtree", "path": "<narrowest dir that must change>"}],
  "acceptanceCriteria": [
    "<observable outcome 1>",
    "<observable outcome 2>"
  ],
  "requiredValidation": [
    {"argv": ["<test command>", "<arg>"], "cwd": ".", "purpose": "project tests"}
  ]
}
GROK_CONTRACT
```

Then add `--contract-file "$CONTRACT_FILE"` to the code call. Rules:
- `target` must equal `--target` exactly (the wrapper rejects mismatches).
- Scope paths are repo-relative, no `..`, no absolute paths.
- Omit `requiredValidation` if you do not know a safe project test command -
  the workspace build gate still runs.
- If the user's ask has no crisp outcomes, ask them once, or proceed without
  a contract and say so.
```

- [ ] **Step 2: Mirror in the Codex TOML** (`developer_instructions` gains a condensed version of the same block; keep TOML escaping valid - triple-quoted string, no unescaped `"""`).

- [ ] **Step 3: Verify materialization test still passes** (`node --test tests/codex-agents.test.mjs`), run `claude plugin validate ./plugin --strict`, commit:

```bash
git add plugin/agents/grok-engineer-coder.md plugin/codex-agents/grok-engineer-coder.toml plugin/skills/code/SKILL.md
git commit -m "docs: agents derive a default implementation contract before code runs"
```

### Task 1.4: `implement` combo mode (code + auto-handoff, one call)

**Files:**
- Modify: `plugin/scripts/grok-companion.mjs` (new `cmdImplement`, dispatch in `main()` next to `debate` at line ~752)
- Create: `plugin/skills/implement/SKILL.md`, `plugin/skills/implement/run.mjs` (copy any existing `plugin/skills/code/run.mjs` - they are identical self-locating runners)
- Test: `plugin/scripts/tests/implement.test.mjs`

**Interfaces:**
- Consumes: existing `runWithLiveRelay(wrapper, args, track)` (returns Promise<exitCode>), `resolveRunIdFromJobAndStdout`, `runHandoff(wrapper, args)`, `tryParseEnvelope`.
- Produces: companion mode `implement <same args as code>`: runs `code` with live relay, reads the terminal code envelope's `runId`, then runs wrapper `handoff --run-id <id>`. Stdout carries the code envelope then the handoff envelope, sequentially (same precedent as `debate`, which already emits two envelopes). Exit code: 0 only when BOTH code succeeded and handoff reports `response.integration.ready === true`; otherwise 1 (a completed-but-not-ready implement is a nonzero outcome so hosts notice). Direct run-mode: refuse up front with the same fail-closed message as `--contract-file` (handoff artifacts only exist hardened).

- [ ] **Step 1: Write the failing test**

The companion tests already stub the wrapper with fake Python scripts (see how `plugin/scripts/tests/*.test.mjs` fixture wrappers work - reuse that harness). New test:

```js
// plugin/scripts/tests/implement.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
// Reuse the existing companion spawn-harness helpers from the sibling tests
// (look at tests/companion*.test.mjs for runCompanion(argv, {env}) helper or
// equivalent; add one if the suite spawns grok-companion.mjs directly).

test("implement runs code then handoff and exits 0 only when ready", async () => {
  // fake wrapper: on `code` print a success envelope with runId 20260716T000000Z-abc123;
  // on `handoff --run-id 20260716T000000Z-abc123` print a success envelope with
  // response.integration.ready=true. Assert stdout contains both envelopes in
  // order and exit code 0.
});

test("implement exits 1 when handoff not ready", async () => {
  // fake handoff prints ready=false blockers -> expect exit 1, both envelopes still relayed.
});

test("implement refuses in direct run-mode", async () => {
  // GROK_SKILLS_MODE=direct (or workspace prefs) -> stderr message, exit 1, no wrapper spawn.
});
```

Flesh these out against the actual harness in the sibling tests - do not invent a new spawn mechanism if one exists.

- [ ] **Step 2: Run to verify failure** (`node --test tests/implement.test.mjs` -> FAIL, unknown mode).

- [ ] **Step 3: Implement `cmdImplement` in grok-companion.mjs**

```js
async function cmdImplement(cwd, wrapper, rest, runMode, track, staged) {
  if (runMode === "direct") {
    process.stderr.write(
      "[grok-companion] implement requires hardened mode (handoff artifacts do not exist in direct mode). " +
        "Run setup --run-mode hardened, or use plain code in direct mode.\n"
    );
    return 1;
  }
  const codeArgs = ["code", ...rest];
  let stdoutBuf = "";
  // Reuse runWithLiveRelay but capture stdout: today it already accumulates
  // stdoutBuf internally for the job store; extend it to also return the buffer:
  // change finish(code) -> resolve({ code, stdout: stdoutBuf }) behind a new
  // option { captureStdout: true } so existing callers are untouched.
  const res = await runWithLiveRelay(wrapper, codeArgs, { ...track, captureStdout: true });
  const code = typeof res === "number" ? res : res.code;
  stdoutBuf = typeof res === "number" ? "" : res.stdout;
  const env = tryParseEnvelope(stdoutBuf);
  const runId = sanitizeRunId(env?.runId);
  if (!runId) {
    process.stderr.write("[grok-companion] implement: no runId in the code envelope; cannot hand off.\n");
    return code === 0 ? 1 : code;
  }
  stderrLine(`[grok-implement] code finished (exit ${code}); verifying handoff for ${runId}`);
  // handoff envelope goes to stdout via inherit (runHandoff/runPassthrough)
  const handoffCode = runHandoff(wrapper, ["handoff", "--run-id", runId]);
  // Ready detection: runPassthrough inherits stdio, so re-read the durable
  // envelope instead of parsing stdout: runs/<runId>/envelope.json is the
  // wrapper's stored handoff... it is NOT (handoff is read-only). Instead,
  // switch runHandoff here to a captured spawnSync so we can parse ready:
  return handoffCode;
}
```

Design note the implementer must follow (this resolves the comment above): do NOT use `runPassthrough` for the handoff leg. Add a small captured variant:

```js
function runHandoffCaptured(wrapper, args) {
  const result = spawnSync(PYTHON, [wrapper, ...args], {
    encoding: "utf8",
    env: wrapperChildEnv(process.env),
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) {
    process.stderr.write(spawnFailedMessage(wrapper, result.error.message));
    return { code: SPAWN_FAILED_EXIT, envelope: null };
  }
  if (result.stderr) process.stderr.write(result.stderr);
  const stdout = result.stdout || "";
  if (stdout) process.stdout.write(stdout.endsWith("\n") ? stdout : `${stdout}\n`);
  return {
    code: typeof result.status === "number" ? result.status : SIGNAL_EXIT,
    envelope: tryParseEnvelope(stdout),
  };
}
```

Final exit logic in `cmdImplement`:

```js
  const { code: hCode, envelope: hEnv } = runHandoffCaptured(wrapper, ["handoff", "--run-id", runId]);
  const ready = hEnv?.response?.integration?.ready === true || hEnv?.response?.ready === true;
  stderrLine(`[grok-implement] handoff ${ready ? "READY" : "NOT READY"} for ${runId}`);
  return code === 0 && hCode === 0 && ready ? 0 : 1;
```

(Check the actual handoff envelope shape in `modes/handoff.py` - it exposes ready under `response`; match the real key, and mirror it in the test fixtures.)

Dispatch in `main()` after the `debate` branch:

```js
  if (mode === "implement") {
    const wrapper = resolveWrapperPath(process.env);
    if (!wrapper) {
      process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
      return WRAPPER_NOT_FOUND_EXIT;
    }
    const track = { kind: "code", mode: "code", notifyMode: "implement", runMode, skipNotify: noNotify };
    return Promise.resolve(cmdImplement(cwd, wrapper, rest, runMode, track, staged)).then(finishCleanups);
  }
```

(Move the `finishCleanups` definition above this branch, or restructure minimally; keep staged-task cleanup working for implement.)

- [ ] **Step 4: Write `plugin/skills/implement/SKILL.md`**

Frontmatter mirrors `code` (same argument-hint plus a note); body: same transparent-runner preamble as other skills, then:

```markdown
Run a full delegate cycle in one call: Grok `code` in an isolated worktree,
then an automatic `/grok:handoff` verification on the resulting runId. Relay
BOTH envelopes verbatim, in order. Integration readiness comes from the SECOND
(handoff) envelope only. Exit 0 means code succeeded AND handoff is
dual-condition ready. This still never applies, commits, or pushes - parent
apply stays manual (see references/implementation-handoff.md).
Requires hardened mode; direct mode is refused fail-closed.
Foreground/background selection: same AskUserQuestion flow as /grok:code.
```

- [ ] **Step 5: Run suites, validate plugin, commit**

Run: `cd plugin/scripts && node --test tests/*.test.mjs` -> pass. `claude plugin validate ./plugin --strict` -> pass.

```bash
git add plugin/scripts/grok-companion.mjs plugin/scripts/tests/implement.test.mjs plugin/skills/implement
git commit -m "feat: implement combo mode (code + auto-handoff, single call, ready-gated exit)"
```

### Task 1.5: Unified ID acceptance on result/status/cancel

**Files:**
- Modify: `plugin/scripts/lib/jobs.mjs` (add `findJobByRunId`), `plugin/scripts/grok-companion.mjs` (`cmdResult`, `cmdCancel`, status dispatch)
- Test: `plugin/scripts/tests/jobs.test.mjs` (extend)

**Interfaces:**
- Produces: `findJobByRunId(cwd, runId, env) -> job|null` scanning the job index for `job.runId === runId` (newest first). `cmdResult`/`cmdCancel`: when the positional arg matches the strict runId shape (`/^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/` - same regex as `sanitizeRunId`), resolve the job via `findJobByRunId` before falling back to job-id lookup. `status <bare-id>`: when a positional non-flag arg matches the runId shape, treat it as `--run-id <id>`.

- [ ] **Step 1: Failing test**

```js
test("findJobByRunId resolves the newest job carrying that runId", () => {
  // create two jobs via createJob in a temp cwd, updateJob one with runId
  // "20260716T000000Z-abc123", assert findJobByRunId returns it and an
  // unknown runId returns null.
});
```

- [ ] **Step 2: Run to verify failure.** `node --test tests/jobs.test.mjs` -> FAIL.

- [ ] **Step 3: Implement** - in jobs.mjs:

```js
export function findJobByRunId(cwd, runId, env = process.env) {
  if (!runId) return null;
  const jobs = listJobs(cwd, env); // newest-first ordering already used by the table
  return jobs.find((j) => j.runId === runId) || null;
}
```

In `cmdResult`/`cmdCancel` (grok-companion.mjs): before `getJob(cwd, jobId)`, add:

```js
  const RUN_ID_SHAPE = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;
  let job = null;
  if (jobId && RUN_ID_SHAPE.test(jobId)) {
    job = findJobByRunId(cwd, jobId);
  }
  if (!job) job = getJob(cwd, jobId);
```

In the status dispatch (before `parseRunIdArg` check at line ~847): if `wrapperArgs` has a bare positional matching RUN_ID_SHAPE, rewrite it to `["status", "--run-id", id]`.

- [ ] **Step 4: Suites green; update `plugin/skills/result/SKILL.md`, `status`, `cancel` SKILL.mds and README table** ("accepts a job id or a runId - the companion translates").

- [ ] **Step 5: Commit**

```bash
git add plugin/scripts plugin/skills/result plugin/skills/status plugin/skills/cancel README.md
git commit -m "feat: result/status/cancel accept either job id or runId"
```

### Task 1.6: Direct-mode parity (honest boundary + working job surface)

The review's gap #4: direct mode emits `runId: direct-<timestamp>` (`plugin/scripts/lib/direct-grok.mjs:76,101,220`), which fails the strict `RUN_ID_RE`, so `status --run-id`/`result`-by-runId/`handoff` all dead-end. Full handoff artifacts stay hardened-only BY DESIGN (the artifacts' value IS the isolation evidence: worktree, sentinel, sandbox verification - direct mode has none of those to attest). What 2.0.0 fixes is the *parity of the job surface* and the *quality of the refusal*.

**Files:**
- Modify: `plugin/scripts/lib/direct-grok.mjs`, `plugin/scripts/grok-companion.mjs` (handoff/status refusal path), `plugin/scripts/lib/jobs.mjs` (no change expected; verify `findJobByRunId` matches direct ids)
- Test: `plugin/scripts/tests/direct-grok.test.mjs` (extend existing)

**Interfaces:**
- Produces:
  - Direct runs keep their `direct-<timestamp>` id shape but the id is stored on the job record (verify `runDirectGrok` already stores it via `updateJob`; wire if not), and `result`/`cancel`/`status` accept it: extend Task 1.5's `RUN_ID_SHAPE` gate to a second shape `/^direct-[0-9]+$/` that resolves via `findJobByRunId` only (never forwarded to the wrapper).
  - `status --run-id direct-*` and `handoff --run-id direct-*` return a single actionable stderr message + exit 1 BEFORE spawning the wrapper: `"direct-mode runs have no hardened run state. Job output: result <id>. For verified handoff artifacts, rerun with setup --run-mode hardened."` (today the wrapper would reject the id shape with a less helpful usage-error).
  - `implement` refusal in direct mode (Task 1.4) references this same message wording - extract it to a shared constant `DIRECT_NO_HANDOFF_MSG` in `direct-grok.mjs` and import it in the companion (DRY).

- [ ] **Step 1: Failing tests** - (a) direct job's runId resolvable via `result direct-1234567890`; (b) `handoff --run-id direct-1234567890` exits 1 with the message and no wrapper spawn (assert via the fake-wrapper harness that the wrapper script was never invoked); (c) message constant shared (import it in the test from direct-grok.mjs and assert companion stderr contains it verbatim).
- [ ] **Step 2: Run to verify failures.**
- [ ] **Step 3: Implement (companion pre-dispatch check in the WRAPPER_ONLY_MODES branch, `grok-companion.mjs:811-838`).**
- [ ] **Step 4: Suites green.**
- [ ] **Step 5: Docs** - README "Run modes" table gains a "Handoff artifacts" column (hardened: yes; direct: no - by design, with one-line rationale); SECURITY.md model section already implies it, add the explicit sentence. Commit:

```bash
git add plugin/scripts README.md SECURITY.md
git commit -m "feat: direct-mode job-surface parity + honest handoff refusal"
```

### Task 1.7: Phase 1 docs checkpoint (no version bump)

- [ ] CHANGELOG `2.0.0 (unreleased)` additions; README: new `implement` row in the skills table + "Implementation handoff" section notes contract-by-default and contractSummary + direct-mode column; roadmap.md: mark "acceptance criteria wired" + "one-call implement" done-in-2.0-branch. No version bump.

---

# Phase 2 - Iteration loop: `code --continue-run` (PR8)

The single biggest peer-agent gap: every run is one-shot (`_shared.py:531` mints `session_id=str(uuid.uuid4())`; the private home - where the Grok CLI persists its session store - is destroyed in the run's `finally`). This phase archives the session store per run and adds `code --continue-run <runId>` which reuses the retained worktree and resumes the Grok session.

### Task 2.0: Probe - session store layout inside the private home

This repo's discipline: no live-behavior claim without a captured probe. Do this before writing session code.

**Files:**
- Create: `docs/research/2026-XX-XX-session-resume-probe.md`

- [ ] **Step 1: Run a probe** (macOS, hardened prerequisites present):

```bash
# 1. any tiny hardened run, e.g.:
node plugin/scripts/grok-companion.mjs reason --task 'Say the word ready and stop.' --timeout 120
# 2. BEFORE it finishes (or via a temporarily instrumented authhome teardown),
#    inspect the private home:
ls -la "$TMPDIR"/grok-home-*/.grok/ 2>/dev/null
ls -la "$TMPDIR"/grok-home-*/.grok/sessions/ 2>/dev/null
```

Record: (a) does the CLI create `<home>/.grok/sessions/`? (b) file naming (session-id keyed?), (c) whether `grok --session-id <existing>` in a FRESH home with the sessions dir copied in resumes context (probe with two `-p` runs: first says "remember the word pineapple", second asks "what word did I ask you to remember?" with the copied store + same `--session-id`). (d) Whether resume works with `--prompt-file` + `streaming-json` (the wrapper's invocation shape).

- [ ] **Step 2: Write the probe report** with exact commands, CLI version (`grok --version`), and observed layout. Decision gate: if resume-by-copied-store does not work, STOP Phase 2 tasks 2.1-2.3 and fall back to the documented alternative in Task 2.4 (prompt-reconstruction continuation), which needs no session store.

- [ ] **Step 3: Commit** `git add docs/research && git commit -m "docs: session resume probe report"`

### Task 2.1: Archive the session store per run

**Files:**
- Create: `plugin/wrapper/scripts/groklib/session_store.py`
- Modify: `plugin/wrapper/scripts/groklib/modes/_shared.py` (archive before home destroy, line ~789-792; thread `session_id` from ModeRun), `plugin/wrapper/scripts/groklib/modes/_envelope.py` (ModeRun fields)
- Test: `plugin/wrapper/scripts/tests/test_session_store.py`

**Interfaces:**
- Produces:
  - `session_store.archive_session(home_dir: pathlib.Path, run_dir: pathlib.Path, session_id: str) -> Optional[dict]` - copies `<home_dir>/.grok/sessions` (whole dir, if it exists) to `<run_dir>/session/sessions/` (0700 dirs, 0600 files) and writes `<run_dir>/session/session-meta.json` `{"schemaVersion": 1, "grokSessionId": session_id, "archivedAtUtc": iso}`. Returns the meta dict, or None when no sessions dir existed (record a warning upstream, never raise - archival failure must not flip a successful run).
  - `session_store.load_session_meta(run_dir) -> Optional[dict]`; `session_store.seed_sessions(run_dir, home_dir) -> bool` - copies the archived sessions dir into a NEW private home before spawn.
  - `ModeRun` gains `session_id: Optional[str] = None` and `seed_session_from_run_dir: Optional[pathlib.Path] = None` (dataclass fields in `modes/_envelope.py`; confirm field ordering keeps defaults legal).
  - `_execute_and_verify` (`_shared.py:517-535`): `session_id=run.session_id or str(uuid.uuid4())` in the GrokRunSpec; before `grokcli.execute`, if `run.seed_session_from_run_dir` is set, call `seed_sessions(run.seed_session_from_run_dir, home.home_dir)`.
  - In `_run_grok_mode_body`, in the inner `finally` BEFORE `home_cleanup.destroy_once()` (line ~790): archive when the spec had a session id (always true now) - `session_store.archive_session(home.home_dir, run_paths.run_dir, spec_session_id)`; the actual session id used must be captured (store it on the result holder or a one-element list next to `result_holder`, populated in `_execute_and_verify`).

- [ ] **Step 1: Failing tests**

```python
# tests/test_session_store.py
import pathlib, tempfile, unittest
from groklib import session_store

class TestSessionStore(unittest.TestCase):
    def test_archive_and_seed_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            (home / ".grok" / "sessions").mkdir(parents=True)
            (home / ".grok" / "sessions" / "abc.jsonl").write_text("{}\n", encoding="utf-8")
            run_dir = pathlib.Path(td) / "run"
            run_dir.mkdir()
            meta = session_store.archive_session(home, run_dir, "abc")
            self.assertEqual(meta["grokSessionId"], "abc")
            self.assertTrue((run_dir / "session" / "sessions" / "abc.jsonl").is_file())
            home2 = pathlib.Path(td) / "home2"
            home2.mkdir()
            self.assertTrue(session_store.seed_sessions(run_dir, home2))
            self.assertTrue((home2 / ".grok" / "sessions" / "abc.jsonl").is_file())

    def test_archive_missing_sessions_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            home.mkdir()
            run_dir = pathlib.Path(td) / "run"
            run_dir.mkdir()
            self.assertIsNone(session_store.archive_session(home, run_dir, "abc"))
```

- [ ] **Step 2: Run to verify failure** (`python3 -m unittest tests.test_session_store -q` -> import error).

- [ ] **Step 3: Implement `session_store.py`** (~80 lines: `shutil.copytree` with `dirs_exist_ok=False` into a fresh target, chmod walk to 0700/0600, meta json with `os.open(..., 0o600)` like `write_manifest`; all failures logged via `log_stderr("session_store", ...)` and swallowed into a `None`/`False` return). Header comment: state that the archive contains MODEL CONVERSATION CONTENT (prompt + repo excerpts) and inherits the run dir's 0700 confinement; secret redaction does NOT apply here because the archive is never emitted to stdout - document that in SECURITY.md in Step 5.

- [ ] **Step 4: Wire into `_shared.py` + `_envelope.py` as specified in Interfaces; run the FULL wrapper suite** (the lifecycle tests are strict about the finally block - keep archive strictly before destroy, wrapped in try/except).

- [ ] **Step 5: Docs + commit** - SECURITY.md: new subsection "Session archives" (what is stored, where, that `cleanup --run-id --confirm` removes it - verify cleanup already removes the whole run dir; if it does, one sentence suffices).

```bash
git add plugin/wrapper/scripts/groklib/session_store.py plugin/wrapper/scripts/groklib/modes plugin/wrapper/scripts/tests/test_session_store.py SECURITY.md
git commit -m "feat: archive grok session store per run (groundwork for continue-run)"
```

### Task 2.2: `code --continue-run <runId>`

> Hardened by review (Phase 2 findings 2-6): single-lineage `continuedByRunId` CAS claim + concurrent-writer guard, contract integrity pin / missing-copy fail-closed, early baseRevision validity, and MAX_CONTINUATION_ITERATION=20.

**Files:**
- Modify: `plugin/wrapper/scripts/grok_agent.py` (argparse, line ~201-211), `plugin/wrapper/scripts/groklib/modes/code.py` (continuation path in `run()`), `plugin/wrapper/scripts/groklib/code_handoff_finalize.py` (iteration counter in manifest), `plugin/wrapper/scripts/groklib/runstate.py` only if a helper is missing (`load_run_record` exists)
- Test: `plugin/wrapper/scripts/tests/test_mode_code.py`, `plugin/wrapper/scripts/tests/test_implementation_handoff.py`

**Interfaces:**
- CLI: `code --continue-run <runId> (--task ...|--task-file ...)` with `--target/--base/--contract-file` FORBIDDEN alongside it (usage-error; they are derived from the prior run). `--model/--timeout/--max-turns/--web` remain allowed.
- Semantics (each continuation is its OWN run - new runId, own envelope, own handoff artifacts; envelope/manifest integrity untouched):
  1. Load prior `run.json` via `runstate.load_run_record(prior_id)`; require `mode == "code"`, a terminal lifecycle, and `worktreePath` existing on disk; else `GrokWrapperError("invalid-target", "cannot continue run ...", {...})`.
  2. Reuse the prior worktree: rebuild an `ExternalWorktree` from the recorded `worktreePath`/`worktreeBranch`/`baseRevision` (add `worktree_mod.rebuild_external_worktree(repo_root, path, branch, base_revision)` if `cleanup` does not already expose one - `rg -n "rebuild" plugin/wrapper/scripts/groklib/worktree.py` first; cleanup's "rebuild and reap" path suggests a helper exists - reuse it) and `verify_external_worktree` it.
  3. `_prepare` for continuation: NO worktree creation, NO dependency install re-run (node_modules already present check handles it - `_maybe_install_dependencies` is already idempotent, keep calling it), pristine manifest fields MUST come from the committed base, not the edited worktree: implement `_read_committed_manifest_fields_from_ref(worktree_path, base_revision, target_relative)` using `git -C <worktree> show <base>:<target>/package.json` (empty/missing -> `(None, None)` same as today).
  4. Session: `session_meta = session_store.load_session_meta(prior_run_dir)`; when present, set `ModeRun.session_id = meta["grokSessionId"]` and `seed_session_from_run_dir = prior_run_dir`; when absent, continue with a fresh session and append warning `"prior run has no session archive; continuing in the same worktree with a fresh Grok session"`.
  5. Prompt: sentinel directive (new sentinel for the NEW run id) + contract directive (from the PRIOR run's manifest `contractSummary` if present, rendered read-only) + a continuation preamble:

```python
def _continuation_directive(prior_run_id: str, prior_iteration: int) -> str:
    return (
        "This is iteration {} continuing run {}. You are in the SAME isolated "
        "worktree as before; your prior changes are present. Apply the follow-up "
        "instructions below to the existing work. Do not revert prior progress "
        "unless the instructions say so.\n\n".format(prior_iteration + 1, prior_run_id)
    )
```

  6. run.json for the new run gains `"continuesRunId": prior_id` and `"iteration": prior_iteration + 1` (prior_iteration read from the prior run.json, default 0). `code_handoff_finalize` writes `"iteration"` and `"continuesRunId"` into the manifest (validator: optional int >= 1 / optional runId-shaped string; same permissive-when-absent pattern as contractSummary).
  7. Finalize path is UNCHANGED (sentinel, scopes from prior contract if one was recorded - see note below, patch base..worktree cumulative, build gate with the ref-read pristine scripts, dual-condition ready).

Contract on continuation: the original `--contract-file` content is not on disk in the run dir today. Persist it: in Task 2.2 also write `runs/<runId>/contract.json` (the normalized contract, 0600) during the initial code run when a contract was provided (one `json.dumps` next to the existing artifact writes in `code_handoff_finalize.py`), and on continuation load it from the prior run dir so writeScopes/requiredValidation keep applying. No contract file -> no contract, same as today.

- [x] **Step 1: Failing tests** (representative set - write them all before implementing):

```python
# tests/test_mode_code.py
def test_continue_run_rejects_target_and_base(self):
    # parse ["code", "--continue-run", RID, "--target", ".", "--task", "x"]
    # via grok_agent._build_parser + the new validation -> usage-error envelope.

def test_continue_run_unknown_run_id_fails_invalid_target(self): ...
def test_continuation_directive_names_iteration_and_prior_run(self):
    text = code_mode._continuation_directive("20260716T000000Z-abc123", 1)
    self.assertIn("iteration 2", text)
    self.assertIn("20260716T000000Z-abc123", text)
def test_read_committed_manifest_fields_from_ref(self):
    # fixture repo: commit package.json with scripts, edit it in the worktree,
    # assert the ref-read returns the COMMITTED name/scripts.
```

```python
# tests/test_implementation_handoff.py
def test_manifest_iteration_and_continues_fields_validated(self):
    doc = self.make_valid_manifest()
    doc["iteration"] = 2
    doc["continuesRunId"] = "20260716T000000Z-abc123"
    self.assertEqual(validate_implementation_handoff(doc), [])
    doc["iteration"] = 0
    self.assertTrue(any("iteration" in e for e in validate_implementation_handoff(doc)))
```

- [x] **Step 2: Run to verify failures.**

- [x] **Step 3: Implement in this order** (each keeping the suite green): (a) argparse flag + mutual-exclusion validation in `code.run()` head; (b) `_read_committed_manifest_fields_from_ref`; (c) contract persistence to `runs/<id>/contract.json`; (d) continuation branch in `run()` building `WorktreePrep` from the rebuilt worktree (this requires `run_worktree_mode` to accept a pre-existing worktree - add a `existing_worktree: Optional[ExternalWorktree]` parameter that skips creation but keeps every verify/finalize step; read `modes/_worktree.py:209` before deciding the exact seam); (e) session meta load + ModeRun fields; (f) run.json + manifest iteration fields.

- [x] **Step 4: Full wrapper suite green.**

- [ ] **Step 5: Live smoke on this repo** (hardened, macOS): initial `code` run with a trivial task, then `code --continue-run <id> --task 'Also update the comment above the function you changed.'`, then `handoff --run-id <new id>` -> ready. Record in `plugin/references/manual-smoke.md` as a new numbered scenario.

- [ ] **Step 6: Commit**

```bash
git add plugin/wrapper/scripts plugin/references/manual-smoke.md
git commit -m "feat: code --continue-run resumes the retained worktree and grok session"
```

### Task 2.3: Surface continue-run in skills + agents

**Files:**
- Modify: `plugin/skills/code/SKILL.md` (argument-hint + continuation section), `plugin/agents/grok-engineer-coder.md`, `plugin/codex-agents/grok-engineer-coder.toml`, `plugin/skills/handoff/SKILL.md` (not-ready guidance now says: prefer `--continue-run` over a fresh run), README.md
- Test: `plugin/scripts/tests/codex-agents.test.mjs` still green after TOML edit

- [ ] **Step 1: code SKILL.md** - add to argument-hint `[--continue-run <runId>]`; add section:

```markdown
## Iterating on a run (2.0.0+)

When a handoff is NOT ready, or review feedback needs applying, do not start
over. Continue the same run:

```bash
node "$SKILL_BASE/run.mjs" code --continue-run '<runId>' --task-file - <<'GROK_TASK'
<follow-up instructions, e.g. the handoff blockers or review findings to fix>
GROK_TASK
```

Rules: --target/--base/--contract-file are derived from the original run and
must be omitted. Each continuation returns a NEW runId; hand off with THAT id.
```

- [ ] **Step 2: grok-engineer-coder (both hosts)** - replace step 6-7 of the "After a code run" list with: on not-ready handoff, summarize `integration.blockers`, then `code --continue-run` with the blockers as the task; re-handoff; give up after 2 continuations and report.

- [ ] **Step 3: Suites + `claude plugin validate ./plugin --strict` + commit.**

```bash
git add plugin/skills plugin/agents plugin/codex-agents README.md
git commit -m "docs: continue-run iteration loop in skills and agents"
```

### Task 2.4: Fallback (ONLY if probe 2.0 fails): prompt-reconstruction continuation

If session resume by copied store does not work on the current CLI, implement `--continue-run` identically EXCEPT session seeding: instead, the continuation prompt embeds (a) the prior task text (from `runs/<id>/prompt.txt`, which persists) and (b) `git -C <worktree> diff <base> --stat` output, under a "## Prior iteration context" heading, then the follow-up task. Everything else (worktree reuse, iteration counter, contract persistence) is unchanged. Decide at Task 2.0's gate; do not build both.

### Task 2.5: Phase 2 docs checkpoint (no version bump)

- [ ] CHANGELOG `2.0.0 (unreleased)` additions; README "Iterating on a run" subsection under Implementation handoff; roadmap.md marks the iteration loop done-in-2.0-branch (this is the "Wave 3 multi-agent" opener). No version bump.

---

# Phase 3 - Claude Code native surface (PR9)

Every task in this phase MUST start by verifying the host feature against current docs (`claude-code-guide` agent or code.claude.com/docs) - these surfaces are new in 2026 and move fast. If a feature is absent on the user's pinned Claude Code version, ship the task behind graceful degradation (feature detect, never hard-require).

### Task 3.1: `bin/` companion shim

**Files:**
- Create: `plugin/bin/grok-skills` (node script, executable bit)
- Modify: `plugin/agents/grok-engineer-coder.md`, `plugin/agents/grok-rescue.md` (prefer the shim), `plugin/references/plugin-root.md`
- Test: `plugin/scripts/tests/bin-shim.test.mjs`

**Interfaces:**
- Produces: a bare `grok-skills <mode> [args...]` command on the Bash tool's PATH while the plugin is enabled (Claude Code plugin `bin/` support). The shim self-locates exactly like `run.mjs`:

```js
#!/usr/bin/env node
// plugin/bin/grok-skills
//
// PATH shim for Claude Code plugin bin/ support: forwards argv to the
// companion in THIS install tree (self-locating; never trusts env roots).
// Codex has no plugin bin support - skills keep using $SKILL_BASE/run.mjs.
import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const companion = path.join(here, "..", "scripts", "grok-companion.mjs");
const result = spawnSync(process.execPath, [companion, ...process.argv.slice(2)], {
  stdio: "inherit",
});
process.exit(typeof result.status === "number" ? result.status : 1);
```

- [ ] **Step 1: Verify the manifest key** - check current plugins-reference docs for whether `bin/` is auto-discovered or needs a manifest field; add the field to `plugin/.claude-plugin/plugin.json` only if required.
- [ ] **Step 2: Failing test** - `bin-shim.test.mjs`: spawn `plugin/bin/grok-skills jobs` in a temp cwd, assert exit 0 and the jobs-table header appears on stdout (jobs works without a wrapper).
- [ ] **Step 3: Create the shim, `chmod +x plugin/bin/grok-skills`, test green.**
- [ ] **Step 4: Agents prefer the shim** - in `grok-engineer-coder.md`, replace the env-guard block (lines 20-29) with:

```bash
if command -v grok-skills >/dev/null 2>&1; then
  GROK_RUN() { grok-skills "$@"; }
else
  PLUGIN_INSTALL="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-}}"
  [ -n "$PLUGIN_INSTALL" ] && [ -f "$PLUGIN_INSTALL/agents/run.mjs" ] || {
    echo "grok-skills shim not on PATH and plugin root not set" >&2; exit 127; }
  GROK_RUN() { node "$PLUGIN_INSTALL/agents/run.mjs" "$@"; }
fi
```

and use `GROK_RUN <mode> ...` in every example. NOTE: the agent's `tools: Bash(node:*)` allowlist must widen to `Bash(node:*), Bash(grok-skills:*)` - update the frontmatter.
- [ ] **Step 5: `claude plugin validate ./plugin --strict`; live check in a Claude Code session (`/reload-plugins`, ask for `grok-skills jobs` via Bash); update plugin-root.md ("the shim is entrypoint #1 on Claude Code; run.mjs everywhere else"); commit.**

```bash
git add plugin/bin plugin/.claude-plugin/plugin.json plugin/agents plugin/references/plugin-root.md plugin/scripts/tests/bin-shim.test.mjs
git commit -m "feat: grok-skills bin shim on Claude Code PATH (single entrypoint)"
```

### Task 3.2: Persistent state via CLAUDE_PLUGIN_DATA

**Files:**
- Modify: `plugin/scripts/lib/jobs.mjs` (`stateRoot`, line ~90)
- Test: `plugin/scripts/tests/jobs.test.mjs` (extend)

**Interfaces:**
- Produces: `stateRoot(cwd, env)` prefers `env.CLAUDE_PLUGIN_DATA` (join with a per-workspace hash segment identical to today's scheme) when set and absolute; falls back to the current location otherwise. One-time migration: if the legacy dir exists and the new one does not, copy the index + prefs forward (best-effort, warning on stderr). The WRAPPER's XDG state root is untouched (runs/<id> stays XDG - it is shared with Codex and the wrapper owns it).

- [ ] **Step 1: Failing test** - with `CLAUDE_PLUGIN_DATA=/tmp/x` in env, `jobsDir(cwd, env)` starts with `/tmp/x/`; without it, unchanged path.
- [ ] **Step 2-3: Implement + green.** Read `stateRoot` first; keep its workspace-keying exactly (jobs are per-workspace by design).
- [ ] **Step 4: Docs** - `plugin/references/README.md` state-layout note; commit.

```bash
git add plugin/scripts/lib/jobs.mjs plugin/scripts/tests/jobs.test.mjs plugin/references/README.md
git commit -m "feat: companion job state honors CLAUDE_PLUGIN_DATA when present"
```

### Task 3.3: SubagentStop handoff nudge hook

**Files:**
- Create: `plugin/scripts/subagent-stop-hook.mjs`
- Modify: `plugin/hooks/hooks.json`
- Test: `plugin/scripts/tests/subagent-stop-hook.test.mjs`

**Interfaces:**
- Produces: a `SubagentStop` hook that reads the hook event JSON from stdin; when the stopping subagent is `grok:grok-engineer-coder` (match on the agent name field in the event payload - verify the exact field name against current hooks docs before coding), it scans the workspace job index (`listJobs`) for the newest `kind: "code"` job with a runId whose run dir lacks a consumed-handoff marker, and emits additionalContext output (JSON on stdout per hooks contract) reminding the host: `"Grok code run <runId> finished. Before integrating, run handoff --run-id <runId> and require dual-condition ready."` Exit 0 always (non-blocking); 5s timeout in hooks.json. When the payload shape is unrecognized, print nothing and exit 0.

- [ ] **Step 1: Verify the SubagentStop payload + output contract in current hooks docs. Record findings in the file header.**
- [ ] **Step 2: Failing test** - pipe a fixture event JSON into the hook with a temp jobs index containing a code job; assert stdout carries the runId reminder; pipe garbage -> silent exit 0.
- [ ] **Step 3: Implement (~60 lines; reuse `listJobs` from lib/jobs.mjs).**
- [ ] **Step 4: hooks.json**:

```json
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "node \"${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT}}/scripts/subagent-stop-hook.mjs\"",
            "timeout": 5,
            "statusMessage": "Grok handoff reminder"
          }
        ]
      }
    ],
```

- [ ] **Step 5: Suites + `claude plugin validate ./plugin --strict`; note in README that Codex may require `/hooks` trust for the new hook; commit.**

```bash
git add plugin/scripts/subagent-stop-hook.mjs plugin/hooks/hooks.json plugin/scripts/tests/subagent-stop-hook.test.mjs README.md
git commit -m "feat: SubagentStop hook nudges handoff after grok-engineer-coder runs"
```

### Task 3.4: userConfig for run mode + notifications

**Files:**
- Modify: `plugin/.claude-plugin/plugin.json`, `plugin/scripts/lib/jobs.mjs` (`getRunMode`/`getNotificationConfig` read env overrides), `plugin/scripts/lib/notification-modes.mjs` if it owns mode parsing
- Test: `plugin/scripts/tests/jobs.test.mjs` (extend)

**Interfaces:**
- Manifest addition (verify exact schema against current plugins-reference first; adjust key names to the documented shape):

```json
  "userConfig": {
    "runMode": {
      "type": "string",
      "enum": ["hardened", "direct"],
      "default": "hardened",
      "description": "Security posture for live Grok runs"
    },
    "notificationMode": {
      "type": "string",
      "enum": ["off", "auto", "native", "webhook"],
      "default": "off",
      "description": "Completion signal for background runs"
    },
    "notificationWebhookUrl": {
      "type": "string",
      "default": "",
      "sensitive": true,
      "description": "HTTPS webhook for completion notifications (webhook mode)"
    }
  }
```

- Plumbing: hooks/commands can substitute `${user_config.KEY}`; the companion is invoked by skills (not hooks), so deliver via env: precedence in `getRunMode` becomes `GROK_SKILLS_MODE` env > workspace prefs (setup) > `GROK_USERCONFIG_RUN_MODE` env > default. Then extend the SessionStart hook command in hooks.json to export the substituted values... hooks cannot export env to later Bash calls - instead the SessionStart hook WRITES them into the workspace prefs only when the operator has never run setup (a `source: "userConfig"` stamp field so an explicit `setup --run-mode` always wins). Keep that one-way and idempotent.

- [ ] **Step 1: Verify `userConfig` schema + `${user_config.*}` substitution scope in current docs; if command-hook substitution is rejected (v2.1.207 restriction applies to shell-form), pass values as argv: `"command": "node .../session-lifecycle-hook.mjs SessionStart --user-config-run-mode ${user_config.runMode} ..."` only if documented safe; otherwise SKIP the plumbing and ship manifest-only defaults with a doc note.** This task is the most schema-sensitive in the plan - fail toward doing less.
- [ ] **Step 2-4: Failing test (prefs precedence), implement, suites green, `claude plugin validate --strict`, commit.**

```bash
git add plugin/.claude-plugin/plugin.json plugin/scripts
git commit -m "feat: userConfig-backed defaults for run mode and notifications (Claude Code)"
```

### Task 3.5: Agent frontmatter upgrades + teams smoke

**Files:**
- Modify: `plugin/agents/grok-engineer-coder.md`, `plugin/agents/grok-rescue.md`
- Create: `docs/checklists/agent-teams-smoke.md`

- [ ] **Step 1: Frontmatter** (verify each key is honored for PLUGIN agents in current sub-agents docs; `hooks`/`mcpServers`/`permissionMode` are documented as ignored for plugin agents - do not add those):

```yaml
---
name: grok-engineer-coder
description: >
  (unchanged)
tools: Bash(node:*), Bash(grok-skills:*)
model: haiku
maxTurns: 40
memory: project
---
```

Rationale to record in the body: the agent is a thin relay (shell out, relay envelopes verbatim) - a small fast model is correct and cheap; `maxTurns: 40` bounds runaway relay loops; `memory: project` lets it remember per-repo quirks (package manager, prior runIds, build-gate config). grok-rescue keeps `model` inherit (it summarizes diagnoses - keep the smarter default) but gains `memory: project`.

review reversal: model inherit (agent is an orchestrator)

- [ ] **Step 2: Teams smoke checklist** (`docs/checklists/agent-teams-smoke.md`): with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, spawn a teammate from `grok:grok-engineer-coder`, delegate one tiny implement cycle, confirm SendMessage follow-up reaches it and the handoff protocol holds. Record results + Claude Code version. This is a checklist, not CI.
- [ ] **Step 3: Validate + commit.**

```bash
git add plugin/agents docs/checklists/agent-teams-smoke.md
git commit -m "feat: agent frontmatter (model/maxTurns/memory) + agent-teams smoke checklist"
```

### Task 3.6: Phase 3 docs checkpoint (no version bump)

- [ ] CHANGELOG `2.0.0 (unreleased)` additions; README (shim, userConfig, hook); COMPATIBILITY.md notes minimum Claude Code version per feature + degradation behavior; roadmap. No version bump.

---

# Phase 4 - Codex parity polish (PR10)

### Task 4.1: Project-scoped Codex agents + backup cap

**Files:**
- Modify: `plugin/scripts/lib/codex-agents.mjs` (`backupAgentFile` line ~141, `ensureCodexAgents` line ~394, `installCodexAgents` line ~165), `plugin/scripts/lib/companion-setup.mjs` (setup flags)
- Test: `plugin/scripts/tests/codex-agents.test.mjs`

**Interfaces:**
- Produces: `setup --codex-agents-scope user|project` (persisted in workspace prefs; default `user` = today's behavior). `project` installs managed TOMLs into `<cwd>/.codex/agents/` instead of `~/.codex/agents/` (verify Codex reads project agents from `.codex/agents/` against current Codex docs before wiring; record the doc URL in the code comment). `backupAgentFile` keeps at most 3 backups: after writing a new `.bak.N`, delete managed-agent backups beyond the 3 newest (managed only - never touch user files).

- [ ] **Step 1: Failing tests** - (a) `installCodexAgents({destDir})` already parameterizes dest; test the scope resolution: prefs `project` -> dest under cwd. (b) create 5 fake `.bak` files for a managed agent, run backup, assert only 3 remain plus the new one.
- [ ] **Step 2-3: Implement + green.**
- [ ] **Step 4: Docs** - README Codex section + setup SKILL.md flag table; commit.

```bash
git add plugin/scripts plugin/skills/setup README.md
git commit -m "feat: project-scoped codex agents option + managed backup cap"
```

### Task 4.2: Codex agent TOML polish + trust documentation

**Files:**
- Modify: `plugin/codex-agents/grok-engineer-coder.toml`, `plugin/codex-agents/grok-rescue.toml`, README.md, `plugin/skills/setup/SKILL.md`

- [ ] **Step 1: TOML** - add `nickname_candidates = ["Grok Coder", "Second Mind"]` (engineer) / `["Grok Rescue"]`; confirm `sandbox_mode = "read-only"` still permits the Bash node shell-out on current Codex (live check; if read-only blocks spawning node, document the required mode instead of silently widening).
- [ ] **Step 2: Trust parity honesty** - README "Stop gate hooks" row and setup SKILL.md gain an explicit paragraph: on Codex, plugin hooks are skipped until trusted via `/hooks`; the stop gate and the SubagentStop nudge are therefore dormant-by-default on Codex. Track openai/codex#18988 (plugin agents) in a new `docs/COMPATIBILITY.md` "Upstream gaps" table with issue links so releases re-check them.
- [ ] **Step 3: `node --test tests/codex-agents.test.mjs` green (materialization of edited TOML), commit.**

```bash
git add plugin/codex-agents README.md plugin/skills/setup docs/COMPATIBILITY.md
git commit -m "docs+feat: codex agent nicknames, trust-parity honesty, upstream gap tracking"
```

---

# Phase 5 - ACP peer channel (PR11, probe-gated experimental)

Order inside this phase: EVIDENCE (5.1) -> DESIGN (5.2) -> IMPLEMENTATION behind an experimental flag (5.3, only on a go decision from 5.1). This matches the repo's probe-before-claim discipline and the PR4 adversarial review's F14 note (ACP is complementary, not a patch-handoff replacement). If the probe is a no-go on the current Grok CLI, 5.3 is skipped, the spec records why, and 2.0.0 ships without the experimental channel - the release notes then say exactly that (honest-limits rule, Non-negotiable #15).

### Task 5.1: ACP probe

**Files:**
- Create: `docs/research/2026-XX-XX-acp-probe.md`, `plugin/wrapper/scripts/tools/acp_probe.py` (probe-only tool, NOT wired into modes; header comment says so)

- [ ] **Step 1: Write `acp_probe.py`** - stdlib-only script that: creates a private home the same way `_shared` does (import `authhome.create_private_home` + `render_config_toml`), spawns `grok agent stdio` with the minimal env (`grokcli._minimal_env` pattern), performs the ACP JSON-RPC handshake over stdin/stdout (initialize, authenticate if required, session/new, one `session/prompt` with a trivial prompt, read streamed chunks, session/cancel), printing every frame to stderr and a summary JSON to stdout. Timeout 120s, kill tree on exit.
- [ ] **Step 2: Run on macOS with a logged-in CLI; capture** - (a) does stdio ACP work under a private HOME? (b) which sandbox/tool-allowlist controls exist per-session in ACP (equivalent of `--tools`, `--sandbox`)? (c) can a session be resumed across process restarts (session/load)? (d) streaming chunk shape vs the current progress.jsonl events.
- [ ] **Step 3: Probe report** with frames transcript (secrets scrubbed), CLI version, and a go/no-go against these acceptance questions: hardened-home compatible? per-session tool confinement equivalent or better than C6 argv? cancellation clean?

```bash
git add docs/research plugin/wrapper/scripts/tools/acp_probe.py
git commit -m "research: ACP stdio probe under the hardened private home"
```

### Task 5.2: 2.0 design spec

**Files:**
- Create: `docs/specs/2026-XX-XX-acp-peer-channel-design.md`

- [ ] **Step 1: Write the spec** covering: (a) architecture - companion keeps one long-lived ACP child per "peer session", wrapped in the SAME guarantees (private home per session, sandbox verification cadence, secret redaction applied to every relayed chunk, worktree cwd for write sessions); (b) surface - `peer start|prompt|status|stop` companion modes and how grok-engineer-coder uses them for real-time steering; (c) how handoff artifacts still gate integration (ACP session ends -> same finalize + manifest path; ACP never replaces the patch protocol); (d) failure model mapping ACP errors to the existing ERROR_CLASS taxonomy; (e) dual-host behavior (works identically from Codex since it is companion-internal); (f) explicit non-goals (no host-side ACP server, no auto-apply, no bypass of one-envelope for one-shot modes).
- [ ] **Step 2: Have it adversarially reviewed** (dogfood: `/grok:adversarial-review --target docs/specs/... --task 'Attack this ACP design: isolation regressions, redaction gaps, lifecycle leaks.'`).
- [ ] **Step 3: Commit.**

```bash
git add docs/specs
git commit -m "docs: ACP peer-channel design spec (probe-gated)"
```

### Task 5.3: Experimental `peer` channel implementation (ONLY on a 5.1 go decision)

Ships in 2.0.0 behind `GROK_EXPERIMENTAL_ACP=1` (companion refuses `peer` modes without it, with a one-line pointer to the spec). Everything below conforms to whatever 5.2's reviewed spec says where they differ - the spec is authority; this task pins the surface so the release scope is concrete.

**Files:**
- Create: `plugin/wrapper/scripts/groklib/acp.py` (JSON-RPC framing + session lifecycle client, stdlib only), `plugin/wrapper/scripts/groklib/modes/peer.py` (new wrapper modes), `plugin/wrapper/scripts/tests/test_acp.py`, `plugin/wrapper/scripts/tests/test_mode_peer.py`, `plugin/skills/peer/SKILL.md`, `plugin/skills/peer/run.mjs`
- Modify: `plugin/wrapper/scripts/grok_agent.py` (subcommands), `plugin/scripts/grok-companion.mjs` (dispatch + experimental-flag gate), `plugin/wrapper/scripts/groklib/modes/__init__.py` (MODES registry)

**Interfaces (surface pinned for 2.0.0):**
- `peer-start --target <path> --base <rev> [--model ...]` - creates run id + private home + external worktree (same `_prepare` guarantees as code), spawns `grok agent stdio` INSIDE that env, performs initialize/session-new, then DETACHES: writes `runs/<runId>/peer.json` (`{pid, sessionId, socketOrStdioInfo, lifecycle: "peer-active"}`) and emits ONE envelope (`status: "running"`, `response.peer.sessionId`). The child outlives the wrapper process via the same spawn-in-own-group machinery as today's runs.
- `peer-prompt --run-id <id> (--task|--task-file)` - reattaches (per the transport 5.1 proved: either a persistent stdio bridge process or session/load on a fresh child - the spec decides), sends one prompt, relays streamed chunks to `progress.jsonl` (SAME redaction pipeline as the existing relay - reuse `groklib.redaction`), emits one envelope per prompt turn with the turn's final text.
- `peer-stop --run-id <id>` - session/cancel + child teardown + THE EXISTING code-mode finalize path (sentinel is not applicable; scopes/patch/build-gate/manifest ARE): a stopped peer session with changes produces the same `implementation-handoff.json` + patch artifacts as a code run, so integration still goes through `/grok:handoff`. Private home destroyed here (peer is the one lifecycle where home outlives a single wrapper invocation - `peer.json` ownership + the stale-home reaper window must be extended for peer-active runs; spec covers this, tests must too).
- Companion: `peer <start|prompt|stop> ...` maps to the wrapper modes; hardened-only; refused without the env flag.

- [ ] **Step 1: TDD the framing client** (`test_acp.py`): frame encode/decode against a fake stdio peer (a Python subprocess echoing canned JSON-RPC), initialize handshake happy path, timeout classification (`GrokWrapperError("acp-failure", ...)` - add the error class to the envelope taxonomy docs).
- [ ] **Step 2: TDD peer.json lifecycle** (`test_mode_peer.py`): start writes peer.json + running envelope; prompt against a dead pid fails closed `acp-failure` with a reattach hint; stop finalizes artifacts + destroys home + terminal envelope; stale-home reaper does NOT reap a peer-active home younger than its lease.
- [ ] **Step 3: Implement acp.py, then peer.py, keeping each green; register modes; companion dispatch + flag gate; skill doc.**
- [ ] **Step 4: Full suites + live smoke on this repo (start -> two prompts -> stop -> handoff ready) recorded in manual-smoke.md.**
- [ ] **Step 5: Commit.**

```bash
git add plugin/wrapper/scripts plugin/scripts plugin/skills/peer plugin/references/manual-smoke.md
git commit -m "feat: experimental ACP peer channel (start/prompt/stop) behind GROK_EXPERIMENTAL_ACP"
```

### Task 5.4: Roadmap update

- [ ] Rewrite `docs/roadmap.md`'s "Recommended next order" around the 2.0.0 thesis: iteration loop + host-native surface + peer channel shipped together; Linux sandbox probe stays the parallel platform track; post-2.0 backlog = peer-channel graduation criteria (when the experimental flag drops), notify follow-ups if re-slated, official directory listings. Commit.

---

# Phase 6 - Manifest polish + release 2.0.0 (PR11, final)

### Task 6.1: Manifest polish + dual-manifest drift guard

Non-negotiable #2 (DRY) applied to packaging: the Claude and Codex manifests are two hand-maintained files that must never drift on shared facts. A generator conflicts with Non-negotiable #7 (the repo tree IS the install - no build step), so the mechanism is a **drift test**, not codegen.

**Files:**
- Modify: `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`
- Create: `plugin/scripts/tests/manifest-parity.test.mjs`

**Interfaces:**
- Parity contract (asserted by the test): identical `name`, `version`, `author.name`, `license`, `homepage`, `repository`; keyword sets equal EXCEPT documented host-specific extras (codex adds `"claude-code"`); descriptions may differ only in the host name (test: strip `Claude Code`/`Codex / ChatGPT` tokens, then compare). Version also matches the top of CHANGELOG.md and the versions in `.claude-plugin/marketplace.json` + `.agents/plugins/marketplace.json` (read both; assert every version field RELEASE.md enumerates is identical).

- [ ] **Step 1: Write the failing drift test**

```js
// plugin/scripts/tests/manifest-parity.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(new URL(".", import.meta.url).pathname, "..", "..", "..");
const read = (p) => JSON.parse(fs.readFileSync(path.join(ROOT, p), "utf8"));

test("claude and codex plugin manifests agree on shared facts", () => {
  const claude = read("plugin/.claude-plugin/plugin.json");
  const codex = read("plugin/.codex-plugin/plugin.json");
  for (const key of ["name", "version", "license", "homepage", "repository"]) {
    assert.deepEqual(claude[key], codex[key], `manifest drift on ${key}`);
  }
  assert.equal(claude.author.name, codex.author.name);
  const claudeKw = new Set(claude.keywords);
  const codexKw = new Set(codex.keywords.filter((k) => k !== "claude-code"));
  assert.deepEqual([...claudeKw].sort(), [...codexKw].sort(), "keyword drift");
});

test("manifest version matches marketplaces and changelog", () => {
  const version = read("plugin/.claude-plugin/plugin.json").version;
  const changelog = fs.readFileSync(path.join(ROOT, "CHANGELOG.md"), "utf8");
  assert.ok(
    changelog.includes(`## ${version}`) || changelog.includes(`## [${version}]`),
    `CHANGELOG.md has no ${version} section`
  );
  for (const mp of [".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"]) {
    const doc = read(mp);
    const text = JSON.stringify(doc);
    assert.ok(text.includes(`"${version}"`), `${mp} does not carry version ${version}`);
  }
});
```

(Adjust the marketplace assertion to the files' real schema after reading them - assert the SPECIFIC version fields, not a substring, once the shape is known; the substring form above is the failing-first draft.)

- [ ] **Step 2: Run to verify it fails or passes for the RIGHT reasons** (it should pass on shared facts today at 1.6.0, then guard Phase 6's bump; the CHANGELOG assertion fails until Task 6.2 finalizes the section - order the test file after 6.2 in CI reality by committing them together, or mark the changelog test with the version from the manifest so it is self-consistent at all times - the draft above already is).
- [ ] **Step 3: Polish the manifest content (both files, same commit):**
  - Descriptions: rewrite to lead with the peer-implementer story: `"Grok as a peer implementer agent for <host>: delegate code to an isolated worktree, iterate with continue-run, integrate through verified handoff. Review, reason, and verify modes included. Not affiliated with xAI."`
  - Keywords: add `"peer-agent"`, `"implementation-handoff"`, `"worktree"` to BOTH (keep the codex-only `"claude-code"` extra).
  - Claude manifest: keep `displayName: "Grok Skills"`; add nothing speculative - every field must be in the current plugins-reference (verify `userConfig` from Task 3.4 landed here; confirm no `bin` field is needed or add the documented one from Task 3.1).
  - Codex `interface` block: refresh `shortDescription`/`longDescription` for 2.0 (continue-run, implement, peer channel experimental); extend `defaultPrompt` with `"Use the grok implement skill to fix <bug> and verify the handoff."`; populate `screenshots` only if you actually add images under `plugin/assets/` (empty array is honest, keep it otherwise).
- [ ] **Step 4: `node --test tests/manifest-parity.test.mjs` green; `claude plugin validate ./plugin --strict` green; commit.**

```bash
git add plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json plugin/scripts/tests/manifest-parity.test.mjs
git commit -m "feat: manifest polish + dual-manifest drift guard test"
```

### Task 6.2: Release 2.0.0

Follow docs/RELEASE.md exactly; this task only enumerates the 2.0-specific content.

- [ ] **Step 1: Finalize CHANGELOG.md** - convert `## 2.0.0 (unreleased)` to the release heading with date; organize by theme: Iteration loop (continue-run, session archive), Delegation (implement, contract-by-default, contractSummary, unified IDs, direct-mode parity), Host surfaces (bin shim, CLAUDE_PLUGIN_DATA, SubagentStop hook, userConfig, agent frontmatter), Codex (project-scoped agents, backup cap, nicknames, trust honesty), Experimental (ACP peer channel + flag, or the honest "probed, not shipped" note per Task 5.1's gate), Hygiene (redaction split, DRY, divergence warning, argv-safety reference), Packaging (manifest polish + drift guard). Breaking-change section: state explicitly whether any surface broke (target: NONE - everything in this plan is additive; if an implementer deviated, list it here).
- [ ] **Step 2: Bump every version RELEASE.md enumerates to 2.0.0** (both `plugin/*/plugin.json`, both marketplace manifests, any version string RELEASE.md's checklist names). The Task 6.1 drift test must pass after the bump.
- [ ] **Step 3: Full gates**: wrapper suite, plugin suite, `claude plugin validate ./plugin --strict`.
- [ ] **Step 4: Update README top matter** for 2.0 (peer-implementer lead paragraph, skills table complete: implement, peer, continue-run flag), verify every doc link resolves (`rg -o "\]\(([^)#]+)" README.md` spot-check).
- [ ] **Step 5: Tag + publish per RELEASE.md** (annotated `v2.0.0`, GitHub Release, dual-host post-smoke: fresh install on Claude Code AND Codex, run preflight + one implement cycle + one continue-run on a scratch repo, record results in `docs/checklists/`).
- [ ] **Step 6: Commit + tag**

```bash
git add -A
git commit -m "release: 2.0.0 peer-agent integration"
# then follow docs/RELEASE.md for the annotated tag + GitHub Release + post-smoke
```

---

## Self-Review (performed at write time; revised for the 2.0.0 single-release scope)

- Spec coverage: all four tiers of the 2026-07-16 review map to phases (T1 items 1-3 -> Phases 1-2; T1 item 4 verified already shipped, dropped with rationale; T2 items 5-10 -> Phase 3; T3 items 11-13 -> Phase 4; T4 item 14 -> Phase 5 with a probe-gated experimental implementation in 5.3; hygiene -> Phase 0; manifest polish + dual-manifest DRY guard -> Task 6.1). Direct-mode (review gap #4) is addressed in Task 1.6 as job-surface parity + honest refusal; full handoff artifacts stay hardened-only by design because the artifacts' value is the isolation evidence direct mode cannot attest - that boundary is documented, not silent.
- Placeholder scan: Tasks 1.2 step 1 (second test), 1.4 step 1, 2.2 step 1, Phase 3 hook payloads, and Task 6.1's marketplace-schema note intentionally direct the implementer to read the existing fixture harness / current host docs / actual file shapes first instead of inventing them - those are verification steps with concrete assertions named, not TBDs. Task 5.3 defers transport details to 5.2's reviewed spec by explicit authority rule. Everything else has real code.
- Type consistency: `_contract_directive` / `_continuation_directive` / `session_store.*` / `findJobByRunId` / `runHandoffCaptured` / `DIRECT_NO_HANDOFF_MSG` names are used consistently across their tasks. `contractSummary` and `iteration`/`continuesRunId` shapes match between writer (code_handoff_finalize), validator (implementation_handoff), and reader (modes/handoff, skills). RUN_ID_SHAPE gains the `direct-` variant in exactly one place (Task 1.5's gate, extended by 1.6).
- Ordering: 0 -> 1 -> 2 are strictly sequential (2.2 consumes 1.1's `_contract_directive` and 2.1's session_store; 1.6 extends 1.5). 3 and 4 are independent of 2 and of each other. 5 depends on 0-2; 5.3 is gated on 5.1's go decision. 6 is last: 6.1's drift test guards 6.2's bump, and no version number changes anywhere before 6.2.
- Version hygiene: no intermediate public versions remain in the plan; user-facing snippets say `2.0.0+`; existing-doc quotes that name historical versions (1.5.0/1.6.0) are quotes, not new claims.
