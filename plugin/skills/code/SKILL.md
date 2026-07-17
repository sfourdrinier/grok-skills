---
name: "code"
description: "Have Grok implement code (default: live tree; opt-in isolated worktree). Nothing is committed or pushed"
argument-hint: "(--target <path> --base <revision> | --continue-run <runId>) (--task <text> | --task-file <path>) [--contract-file <path>] [--web] [--model <id>] [--timeout <s>] [--max-turns <n>]"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool
   (the folder that contains this skill's `SKILL.md` and `run.mjs`).
2. Set `SKILL_BASE` to that path. Do **not** invent versioned cache paths.
3. Always invoke the companion **only** through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
# Required for completion notifications (plugin/references/execution-context.md):
export GROK_COMPANION_EXECUTION_CONTEXT=foreground   # or background
node "$SKILL_BASE/run.mjs" <mode> [args...]
```

`run.mjs` finds the plugin install from its own location and runs
`scripts/grok-companion.mjs`. No `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` required.

If the host already exported `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT`, you may call
`node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs"` instead; prefer
`"$SKILL_BASE/run.mjs"` whenever the Skill tool loaded this skill.

Return companion **stdout verbatim**. Never put free-text in `--task "..."`;
use `--task-file -` with a single-quoted heredoc.

<!-- plugin/skills/code.md -->

Run a Grok `code` implementation through the hardened wrapper and relay its
result envelope. How edits land is **mode-aware** (canonical:
`plugin/references/integration-modes.md`): default **direct** edits the real
tree (hardened-direct; one-time consent); **auto** / **review** use an
external git worktree. The wrapper runs the workspace build gate. Nothing is
ever committed, merged, pushed, or deleted automatically.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- Fresh run: `--target <workspace-relative-path>` and `--base <committed-revision>`
  are required. For **auto/review** the wrapper builds the worktree from a
  committed revision (uncommitted task deps fail closed - commit first). For
  **direct**, `--base` still frames the run; edits land on the live tree
  (dirty-overlap policy applies; see integration-modes.md).
- Optional `--integration direct|auto|review|worktree` (default direct when
  consented; see `plugin/references/integration-modes.md`).
- Continuation: `--continue-run <runId>` instead of `--target`/`--base` (reuses
  the prior retained worktree; mutually exclusive with `--target`, `--base`, and
  `--contract-file`).
- Exactly one of `--task <text>` or `--task-file <path>` is required. Prefer
  `--task-file` for a multi-paragraph spec.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.
- Injection safety (canonical rationale: `plugin/references/argv-safety.md`):
  task text is NEVER placed in a shell-evaluated position - deliver it with
  `--task-file -` and a SINGLE-QUOTED heredoc. Wrap every substituted flag
  VALUE in single quotes (`--target '<path>'`). Bare flags (`--web`) carry no
  value to quote.

`--web` passthrough:
- Web tools are OFF by default. Pass `--web` only when the implementation
  genuinely depends on current external practices, current library or software
  versions, or living external documentation the repo cannot answer. Do not add
  `--web` otherwise.

Execution mode (foreground vs background):
- If the raw arguments include `--wait`, run in the foreground (do not ask).
- If the raw arguments include `--background`, run in a Claude background task
  (do not ask).
- `--wait` and `--background` are Claude Code execution flags. Do NOT forward
  them to the companion or wrapper; strip them from the wrapper argv.
- Otherwise, estimate the size first:
  - Run `git status --short --untracked-files=all` and `git diff --shortstat`.
  - A `code` run is an implementation (default wrapper timeout 3600s), so it is
    almost always long. Recommend background unless the change is clearly tiny.
- Then use `AskUserQuestion` exactly once, recommended option first with its
  label suffixed ` (Recommended)`. The two options are:
  - `Wait for results`
  - `Run in background`

Foreground flow (one Bash call, then relay verbatim). When the arguments carry a
`--task <text>`, route that text through STDIN so it is never shell-evaluated:
```bash
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" code --target '<target from $ARGUMENTS>' --base '<base from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>`, drop the heredoc and pass
every flag as single-quoted argv tokens:
```bash
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" code --target '<target from $ARGUMENTS>' --base '<base from $ARGUMENTS>' --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
```
- Return the command stdout envelope VERBATIM. Do not paraphrase, summarize, or
  add commentary before or after it. Preserve the exit status.

Background flow:
- Set `export GROK_COMPANION_EXECUTION_CONTEXT=background` (see
  `plugin/references/execution-context.md`).
- Launch the same command with `Bash(run_in_background: true)`.
- Do not wait for completion or read its output this turn.
- Tell the user: "Grok code run started in the background. Run `/grok:status
  --run-id <id>` to read the result envelope."

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.

## Implementation contract + handoff (1.6.0+)

**Derive a contract by default** before a non-exploratory `code` run (host
agents: `agents/grok-engineer-coder.md`, Codex
`codex-agents/grok-engineer-coder.toml`). Stage operator-trusted JSON and pass
`--contract-file <path>` (writeScopes + `requiredValidation` argv arrays). Skip
only for exploratory tasks, or when outcomes are not crisp (ask once, or
proceed without a contract and say so). Bad contracts fail closed **before**
Grok with `implementation-contract-invalid`. Trust model:
`operator-contract-trusted-no-os-sandbox` (no OS filesystem sandbox claim for
validation commands).

`requiredValidation` argv is **shell-free** (canonical:
`plugin/references/argv-safety.md`): no globs, no directory shorthands, no
`$VARS`. Prefer **targeted** test modules over a heavy or environment-sensitive
full suite; the workspace build gate still runs. Model examples:
`["node", "--test"]` with `cwd`, and
`["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]`.

While a hardened code run is in flight, do **not** commit or edit the target
checkout (original-checkout guard cannot attribute mid-run divergence);
integrate in a quiet window after the terminal envelope. Changes that add or
move secret-shaped test fixtures cannot produce a handoff patch artifact
(fail-closed scan); expect retained-worktree manual integration
(`references/implementation-handoff.md`).

**Direct run-mode refuses `--contract-file`** (companion fail-closed). Verified
handoff artifacts (`implementation.patch` + `implementation-handoff.json`) are
written only on the **hardened** path under the C2 run dir.

On success or classified failure after hardened Grok, the wrapper writes:

- `runs/<runId>/artifacts/implementation.patch` (immutable git binary full-index)
- `runs/<runId>/implementation-handoff.json`

**Notify is not integrate.** After an isolated (`auto`/`review`) code run,
parents must call `/grok:handoff --run-id <runId from the code envelope>` and
require dual-condition ready before any apply. In **direct**, source edits are
already live. See `skills/handoff/SKILL.md`,
`references/implementation-handoff.md`, and the canonical matrix
`references/integration-modes.md`.

**Integration modes (link only - do not restate):**
`plugin/references/integration-modes.md` - direct (default, live tree + consent),
auto (worktree + apply-on-ready), review (worktree + manual parent apply).

## Iterating on a run (2.0.0+)

When handoff is not ready (or the operator wants a follow-up in the same
worktree), continue instead of starting a fresh run:

```bash
node "$SKILL_BASE/run.mjs" code --continue-run '<runId>' --task-file - <<'GROK_TASK'
<follow-up instructions; e.g. fix the handoff blockers>
GROK_TASK
```

- Do **not** pass `--target`, `--base`, or `--contract-file` with
  `--continue-run` (usage-error). Target, base, and the prior contract are
  derived from the prior run.
- `--model` / `--timeout` / `--max-turns` / `--web` remain allowed.
- Each continuation is a **new** run id with `continuesRunId` + `iteration` on
  `run.json` and the handoff manifest. Handoff the **new** run id before integrate.
- Prefer at most two continuations, then report blockers rather than looping.
- **Single-lineage:** each prior may be continued only once (`continuedByRunId`);
  to iterate further, continue the child run (A->B->C), never fork siblings of A.
- **Contract pinning:** if the prior had a contract, continuation reloads
  `runs/<prior>/contract.json` and requires its sha256 to match the prior
  handoff `contractSha256` (missing or tampered copy fails closed).
- **Iteration cap:** the wrapper refuses a continue that would exceed iteration
  20 (`usage-error`); stop earlier when blockers stop moving.
