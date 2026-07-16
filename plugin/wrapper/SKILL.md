---
name: grok-cli
description: Use when the user says to use Grok, wants a Grok review, wants a second opinion from Grok, or asks to delegate a task to Grok. Also use for getting an independent code review from Grok, having Grok reason about an architecture or debugging question, having Grok implement code in an isolated worktree, or having Grok independently verify someone else's implementation. Trigger phrases include "use grok", "grok review", "second opinion from grok", and "delegate to grok".
---

<!-- plugin/wrapper/SKILL.md -->

# Grok CLI companion agent

This skill wraps the authenticated `grok` CLI in a deterministic Python
wrapper (`scripts/grok_agent.py`) so Grok 4.5 can act as a capable companion
agent for review, reasoning, implementation, and independent verification,
under authority scoped tighter than an interactive session. The wrapper owns
process isolation, private authentication, sandboxing, and result reporting.
It never trusts a Grok narrative in place of verified evidence.

Every subcommand prints exactly one JSON result envelope to stdout, success
or failure alike. Exit code is `0` iff the envelope's `status` is `"success"`.

## Hard rule: copy the command lines exactly

**Never reconstruct a command line for this wrapper from memory or prose.**
The exact set of flags, their order, and their exact spelling are part of
the safety guarantee this wrapper provides (fail-closed argument parsing,
no shell evaluation of task text, a fixed C6 invocation baseline underneath
every mode). Copy a command from this file or from
`references/workflow-patterns.md`, then substitute only the placeholder
values (`<...>`). If none of the documented commands fit the task, stop and
ask before improvising a new flag combination.

Every invocation has this shape:

```bash
python3 plugin/wrapper/scripts/grok_agent.py <mode> [flags...]
```

You can run from any cwd. Relative `--target` / paths resolve against the
**process cwd**; the repository root is the **git toplevel of the resolved
target** (not the wrapper install tree). Marketplace installs live outside the
repo under review.

## The verbatim relay rule

**You must return the wrapper's stdout verbatim.** No paraphrase, no
summary, no added commentary layered on top. The wrapper is the only place
orchestration and safety decisions are made; your job is to run the exact
command and hand back exactly what it printed. If you want to explain the
result in your own words, do that separately, clearly labeled as your own
summary, AFTER relaying the raw envelope - never instead of it.

## The seven subcommands: when to use each

### `preflight` - readiness check, no task

Run this first in a session, or whenever something downstream looks wrong.
It is read-only: it verifies the `grok` binary is runnable, the presence
of `~/.grok/auth.json`, a full private-home create/login/inspect/destroy
cycle, sandbox policy resolution for every live mode, the state root
permissions, and the stale-home audit. It never spawns a task-bearing Grok
run.

```bash
python3 plugin/wrapper/scripts/grok_agent.py preflight
```

### `review` - full-context, read-only code review

Use when local repository rules and surrounding source in a real workspace
matter: reviewing an app, package, or subsystem before merging, or getting
Grok's read on an existing area of the codebase. `review` walks the
repository rules (`AGENTS.md`/`CLAUDE.md`) from repo root down to the target
and includes them in the prompt; it can only read (`read_file`, `grep`,
`list_dir`). Concurrent tree drift or change-shaped JSON keys become
**informational warnings** (findings still returned); write escapes still fail
on `code`/`verify` only.

```bash
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target <workspace-relative-path> \
  --task-file <path-to-task-file>
```

Optional flags: `--web`, `--schema <path>`, `--model <id>` (default
`grok-4.5`), `--timeout <seconds>` (default 900), optional `--max-turns <n>`
(default: **unlimited** - flag omitted unless set). `--task <text>` may replace
`--task-file` for a short prompt; exactly one of `--task` / `--task-file` is required.

### `reason` - isolated artifact reasoning, cold second opinion

Use for architecture or debugging consultation, plan/spec critique, comparing
approaches, or reviewing explicitly-named files or diffs when full
repository context is not required. The working directory is a fresh private
temp dir OUTSIDE the repo and there is no automatic rule discovery - only
files you name with `--input` or `--rules-file` are supplied. This is the
cheapest, most deterministic mode; prefer it over `review` whenever the task
does not depend on neighboring repo files or repo-wide rules.

```bash
python3 plugin/wrapper/scripts/grok_agent.py reason \
  --task-file <path-to-question-file> \
  --input <path-to-artifact>
```

`--input` and `--rules-file` may each be repeated. Optional flags: `--web`,
`--schema <path>`, `--model`, `--timeout` (default 900), optional `--max-turns`
(default: unlimited).

### `code` - implementation in an isolated worktree

Use when Grok should actually write or modify code. The wrapper creates and
verifies an external git worktree itself (never the current checkout),
requires the workspace's full build gate (build or typecheck+lint, plus test
when present) to pass with exit 0, and keeps the worktree for inspection.
Nothing is ever committed, merged, pushed, or deleted automatically.

```bash
python3 plugin/wrapper/scripts/grok_agent.py code \
  --target <workspace-relative-path> \
  --base <committed-revision> \
  --task-file <path-to-spec-file>
```

Optional flags: `--web`, `--model`, `--timeout` (default 3600), optional
`--max-turns` (default: unlimited). If the task depends on uncommitted changes
in the current checkout, `code` fails closed rather than approximating them -
commit what the task needs first.

### `verify` - independent verification, no source edits

Use to have Grok independently inspect and test a change - your own or
someone else's - in an EXISTING worktree, typically one a prior `code` run
produced. Source-editing tools are absent; the run always ends with a
machine-readable `pass` / `fail` / `inconclusive` verdict plus evidence.
`verify` is version 1's independent-verification path (the CLI's own
`--check` verifier surface is deferred; see `references/workflow-patterns.md`
recipe 13).

```bash
python3 plugin/wrapper/scripts/grok_agent.py verify \
  --worktree <absolute-path-to-worktree> \
  --task-file <path-to-verification-task-file>
```

Optional flags: `--model`, `--timeout` (default 1800), optional `--max-turns`
(default: unlimited). `verify` never accepts `--web` - independent verification
stays hermetic by design.

### `status` - read-only inspection of a prior run

Reads back a run's stored envelope and progress stream by run id. It never
writes to the run it inspects.

```bash
python3 plugin/wrapper/scripts/grok_agent.py status --run-id <run-id>
```

### `cleanup` - report or remove a run's owned state

Without `--confirm` this is a dry run: it reports the owned session state
and (for `code`/`verify`) the worktree and branch it WOULD remove. With
`--confirm` it actually removes them, refusing a dirty worktree rather than
forcing it.

```bash
python3 plugin/wrapper/scripts/grok_agent.py cleanup --run-id <run-id>
```

```bash
python3 plugin/wrapper/scripts/grok_agent.py cleanup --run-id <run-id> --confirm
```

## `--task-file` over `--task`

Prefer `--task-file` for anything beyond a short one-line prompt. It avoids
shell-quoting hazards, keeps the task text auditable as a plain file, and is
the only practical way to hand Grok a multi-paragraph spec, a full plan
document, or a review rubric. `--task` and `--task-file` are mutually
exclusive; the wrapper never evaluates task text as shell syntax either way.

## `--web`: opt-in web access

Web tools are OFF by default in every mode, for review determinism (decision
D-WEB). Pass `--web` on `review`, `reason`, or `code` when the task
genuinely depends on current practices, current software or library
versions, or living/external documentation that the repository's own rules
and code cannot answer - for example "does this match the latest stable API
for library X" or "what's the current recommended pattern for Y". When set,
the envelope records `policy.webAccess: true`.

**`verify` never accepts `--web`.** Independent verification stays hermetic
in every case; do not attempt to add web access to a verify run.

## Accepted residual risk and platform gate (read before any live run)

- **Secret-read residual (decision D-SECRETREAD).** Grok 0.2.101 cannot deny
  reads of credential or external-secret paths through any sandbox profile -
  only WRITES are confined. The wrapper mitigates this with per-run private
  `HOME` isolation (so `~` expansions never reach your real home) and by
  isolating the copied `auth.json`, but a Grok child can still read any
  host-readable file by absolute path. Only run live modes against trusted
  inputs. Full evidence: `references/authority-policies.md` and
  `references/cli-reference.md`.
- **macOS-only in version 1 (decision D-PORT).** Live modes (`review`,
  `reason`, `code`, `verify`) fail closed with error class `probe-required`
  on any platform without its own captured Grok sandbox probe report. Only
  macOS has one so far; Linux and Windows stay blocked until their own probe
  suites run.

## Reference material

- `references/authority-policies.md` - per-mode capability tables (tools,
  sandbox, cwd, network, subagents, web) and what every C4 error class means
  for you as the operator.
- `references/cli-reference.md` - last-validated Grok CLI evidence (advisory),
  the C6 invocation baseline, sandbox and permission-mode evidence, auth file
  names, and how maintainers refresh the stamp after a probe suite.
- `references/workflow-patterns.md` - the tested recipes: which mode to use
  for full-context review, cross-file review, isolated diff review,
  multi-angle review, architecture consultation, plan critique, code
  implementation, test generation, plan-conformance review, independent
  verification, structured judgments, and handing a retained worktree to a
  developer or to Codex - plus what is excluded or deferred in version 1.
