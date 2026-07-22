<!-- plugin/wrapper/references/workflow-patterns.md -->

# Workflow patterns

Fourteen tested recipes (spec section 12), each as: when to use it, the
exact command, and what to check in the returned envelope. Every command
below uses only flags that exist in the C8 argparse surface
(`../scripts/grok_agent.py`); do not add a flag that is not documented here
or in `../SKILL.md`.

## 1. Full-context cold file review

**When:** You want Grok's read on a specific file or small area of a
workspace, with the local repo rules in context, and you have no prior
history with Grok on this file.

```bash
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target <workspace-relative-path> \
  --task-file <path-to-review-scope-file>
```

**Check:** `response.text` / `response.structured` for findings;
`instructions[]` lists every `AGENTS.md`/`CLAUDE.md` level actually included
(with byte counts and SHA-256 hashes); `warnings` for anything Grok
flagged in stderr; review may note concurrent file churn as a warning (not a failure). Confirm no
read-only by construction).

## 2. Full-context cross-file or subsystem review

**When:** The review needs to reason about interactions across multiple
files in an app or package, not just one file.

```bash
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target <subsystem-workspace-relative-path> \
  --task-file <path-to-review-scope-file>
```

**Check:** Same as recipe 1, plus confirm `targetWorkspace` in the envelope
matches the intended subsystem root (a `.` target normalizes to the empty
string, meaning the whole repo).

## 3. Isolated artifact or diff review

**When:** You have one or more specific files (a diff, a single module, a
generated artifact) and want Grok's opinion without repo-wide discovery.

```bash
python3 plugin/wrapper/scripts/grok_agent.py reason \
  --task-file <path-to-review-task-file> \
  --input <path-to-artifact-or-diff>
```

Add `--rules-file <path>` (repeatable) if a specific repo rule file should
govern the review; without it the prompt carries no rules block at all.

**Check:** `instructions[]` reflects only the `--rules-file` paths you
selected (empty if none); `policy.tools` shows `["read_file"]` because at
least one `--input` was supplied; `response`.

## 4. Multi-angle review using independent Grok sessions

**When:** You want several independent passes over the same material (for
example a silent-failure pass and a type-design pass) that must not share
context or cancel each other.

Run the same shape of command once per angle, each with its own task file
and, if useful, its own `--schema`:

```bash
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target <workspace-relative-path> \
  --task-file <path-to-angle-1-task-file>
```

```bash
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target <workspace-relative-path> \
  --task-file <path-to-angle-2-task-file>
```

Each invocation is a separate process with its own run id, private home, and
`--leader-socket`, so they are safe to run in parallel; no wrapper flag is
needed to request isolation, it is the default per run (C6/C9).

**Check:** Each run's `runId` and `grok.sessionId` are distinct; no run's
`grok.stopReason` is `Cancelled` from leader contention; consolidate each
run's `response` only after every invocation has returned its own envelope.

## 5. Architecture and debugging consultation

**When:** A bounded design or root-cause question that does not require
editing anything - "should X resolve at import time or call time", "why does
this state go stale on this code path".

```bash
python3 plugin/wrapper/scripts/grok_agent.py reason \
  --task-file <path-to-question-file>
```

Add `--input <path>` (repeatable) for any files the question specifically
references.

**Check:** `response.text` (and `response.structured` if `--schema` was
used); `grok.stopReason == "EndTurn"`.

## 6. Plan or specification critique

**When:** You want an independent critique of a written plan or spec before
executing it.

```bash
python3 plugin/wrapper/scripts/grok_agent.py reason \
  --task-file <path-to-critique-instructions-file> \
  --input <path-to-plan-or-spec-file>
```

**Check:** `response`; if the critique should follow a fixed rubric, pass
`--schema <path>` and check `response.structured` against it.

## 7. Code implementation (mode-aware)

**When:** Grok should actually write or modify code against a specification.
Landing is mode-aware (`--integration`; product matrix:
`plugin/references/integration-modes.md`).

### 7a. Bare wrapper / isolated worktree (safe default)

Omitting `--integration` (or passing `worktree`) creates an external worktree.
This is the fail-closed bare-wrapper default.

```bash
python3 plugin/wrapper/scripts/grok_agent.py code \
  --target <workspace-relative-path> \
  --base <committed-revision> \
  --task-file <path-to-spec-file>
```

**Check:** `worktreePath` / `worktreeBranch` are populated;
`effectiveWorkingDirectory` equals the worktree path (proven by the
`.grok-run-<run-id>` sentinel, never by trusting Grok's narrative);
`commands[]` includes the workspace's full build gate with `exitStatus: 0`
for every required command; `changedFiles` / `diffSummary` for what actually
changed; `cleanup.status == "retained"` (the worktree is never removed on a
successful run).

### 7b. Live-tree direct (product default)

Product companion/skills default to `--integration direct` with **no setup
consent** (2.0.1+). No external worktree; sandbox write root is the repo root.

```bash
python3 plugin/wrapper/scripts/grok_agent.py code \
  --integration direct \
  --target <workspace-relative-path> \
  --base <committed-revision> \
  --task-file <path-to-spec-file>
```

**Check:** edits are on the operator checkout; `worktreePath` is absent/null
for this path; protected-path post-run guards apply. Do not expect a retained
external worktree from product-default direct.

## 8. Test generation and required validation

**When:** The task is specifically to add or extend tests and prove they
pass, not general implementation.

```bash
python3 plugin/wrapper/scripts/grok_agent.py code \
  --target <workspace-relative-path> \
  --base <committed-revision> \
  --task-file <path-to-test-generation-task-file>
```

The task file should explicitly ask Grok to run the new tests; the mode's
own required build gate (which includes `test` when the workspace has a
test script) still runs and is still required regardless of what the task
text says.

**Check:** `commands[]` for the test command's `argv` and `exitStatus: 0`;
`changedFiles` for the new/modified test files.

## 9. Plan-conformance review

**When:** An implementation exists and you want Grok to check it against the
plan or instructions it was built from.

```bash
python3 plugin/wrapper/scripts/grok_agent.py reason \
  --task-file <path-to-conformance-instructions-file> \
  --input <path-to-plan-file> \
  --input <path-to-implementation-summary-or-diff>
```

Use `review --target <workspace> --task-file <...>` instead when the
conformance check genuinely needs live repo context (surrounding files,
current rules) rather than just the named artifacts.

**Check:** `response`; pass `--schema <path>` for a structured per-item
conformance table and check `response.structured`.

## 10. Independent verification

**When:** You want Grok to independently inspect and test a change - your
own or someone else's - in an existing worktree, with no source-editing
tools available to it.

```bash
python3 plugin/wrapper/scripts/grok_agent.py verify \
  --worktree <absolute-path-to-worktree> \
  --task-file <path-to-verification-task-file>
```

**Check:** `verifier.identity` (`grok-<effective-model>`) and
`verifier.verdict` (one of `pass`, `fail`, `inconclusive`); `changedFiles`
should be limited to build/test/cache artifact roots, never tracked source;
a missing or invalid verdict is error class `verifier-unavailable`, not a
result to interpret from prose.

## 11. Structured JSON judgments

**When:** Any of the above tasks needs a machine-parseable verdict rather
than free text - for downstream automation, or to force Grok to commit to a
specific judgment shape.

```bash
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target <workspace-relative-path> \
  --task-file <path-to-task-file> \
  --schema <path-to-json-schema-file>
```

(`reason` also accepts `--schema`; `verify` always uses its own fixed
wrapper-owned verdict schema and needs no `--schema` flag.)

**Check:** `response.structured` holds the object satisfying your schema.
A structured object that fails schema validation is returned as a failure
envelope with error class `schema-mismatch` and a JSON pointer to the
failing location in `error.detail.pointer` - never partially trusted.

## 12. Session continuation when retained context is intentional

**Not available in version 1's command surface.** Every invocation of
`grok_agent.py` generates a fresh `--session-id` (a wrapper-owned UUIDv4)
and a fresh private home for exactly one run; there is no flag to resume or
continue a prior Grok session (see `cli-reference.md`, C6 baseline table).
If retained context genuinely matters, the closest available approximation
with the real flags is: capture the prior run's `response` (via `status
--run-id <id>`) and pass the relevant parts back in as a new `--input` file
on a fresh `reason` call, or fold the follow-up question into the same
`--task-file` as part of one longer single run with a larger `--max-turns`.
Do not invent a `--resume`/`--continue-session` flag; it does not exist.

## 13. `--check` with a confirmed verifier subagent (deferred)

**Deferred in version 1**, per the spec section 12 deferral note. The raw
Grok CLI's `--check` flag depends on subagents and the `task` tool, both
alpha-hazard surfaces (`--check` conflicts with `--no-subagents`, and every
mode in this wrapper always passes `--no-subagents`), and stays outside the
operator surface until a repeatable live probe proves verifier execution,
verdict reporting, and isolation. **Version 1's independent-verification
path is the `verify` mode (recipe 10 above)**, not `--check`. Do not attempt
to pass `--check`-style flags through this wrapper; they are not part of the
C8 surface and will be rejected as a `usage-error`.

## 14. Integrating a code run (handoff + manual apply)

**When:** A hardened `code` run finished and a parent agent or developer needs
to integrate Grok's work. This wrapper never auto-applies, commits, merges,
or pushes (spec section 5.3 + design §14).

```bash
# Optional: re-read terminal envelope / worktree metadata
python3 plugin/wrapper/scripts/grok_agent.py status --run-id <run-id>

# Required before any apply: dual-condition ready (manifest + success mode:code
# envelope + matching baseRevision + non-empty patch size/rehash)
python3 plugin/wrapper/scripts/grok_agent.py handoff --run-id <run-id>
```

Proceed only when the handoff envelope is `status: success` and
`response.integration.ready === true`. Then, as the parent/operator:

1. Re-hash `response.handoff.patchPath` (or manifest `patch.sha256`)
2. `git apply --check --binary <patchPath>` on the intended target checkout
3. Explicit `git apply --binary <patchPath>` only with operator intent
4. Re-run project validation on the parent

Do **not** treat `worktreePath` / `worktreeBranch` as an automatic merge root
and skip handoff - the patch + dual-condition gate is the integration API.
Notify toasts/webhooks are not ready. Dry-run cleanup (no `--confirm`) still
shows what would be removed:

```bash
python3 plugin/wrapper/scripts/grok_agent.py cleanup --run-id <run-id>
```

Only run `cleanup --run-id <run-id> --confirm` after integration is done or
the retained worktree is deliberately discarded.

## Excluded: `--best-of-n`

`--best-of-n` remains experimental on the raw Grok CLI and is excluded from
every recipe above. A live probe of it returned no stdout and is
inconclusive; it is not part of the C8 operator surface and must not be
passed through this wrapper until a repeatable live test proves stdout,
winner selection, model accounting, concurrency isolation, and failure
reporting.
