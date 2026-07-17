---
name: "implement"
description: "One-call delegate: Grok code in an isolated worktree, then auto-handoff verification (never applies)"
argument-hint: "--target <path> --base <revision> (--task <text> | --task-file <path>) [--contract-file <path>] [--web] [--model <id>] [--timeout <s>] [--max-turns <n>]"
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

<!-- plugin/skills/implement.md -->

Run a full delegate cycle in one call: Grok `code` in an isolated worktree,
then an automatic `/grok:handoff` verification on the resulting runId. Relay
BOTH envelopes verbatim, in order. Integration readiness comes from the SECOND
(handoff) envelope only.

**Exit contract:** exit **0** only when code succeeded **and** handoff is
dual-condition ready (`response.integration.ready === true`); exit **1** on
any other outcome (failed code, not-ready handoff, missing runId, spawn
failure). Never propagates a raw spawn exit code.

**Handoff after failed code:** when the code envelope carries a usable
`runId`, handoff **always runs** even if code exited non-zero, so not-ready
blockers surface on stdout. Without a runId, handoff is skipped and the
combo exits 1.

This still never applies, commits, or pushes - parent apply stays manual (see
references/implementation-handoff.md). For **apply-on-verified-ready**, use
`/grok:code --integration auto` (worktree + handoff + apply-time revalidation);
`implement` stays verify-only.
Requires hardened mode; direct mode is refused fail-closed.
Foreground/background selection: same AskUserQuestion flow as /grok:code.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--target <workspace-relative-path>` is required.
- `--base <committed-revision>` is required (the wrapper builds the worktree
  from a committed revision; if the task depends on uncommitted changes, the run
  fails closed - the user must commit what the task needs first).
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
  - An `implement` run is a full code + handoff cycle (default code timeout
    3600s), so it is almost always long. Recommend background unless the change
    is clearly tiny.
- Then use `AskUserQuestion` exactly once, recommended option first with its
  label suffixed ` (Recommended)`. The two options are:
  - `Wait for results`
  - `Run in background`

Foreground flow (one Bash call, then relay verbatim). When the arguments carry a
`--task <text>`, route that text through STDIN so it is never shell-evaluated:
```bash
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" implement --target '<target from $ARGUMENTS>' --base '<base from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>`, drop the heredoc and pass
every flag as single-quoted argv tokens:
```bash
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" implement --target '<target from $ARGUMENTS>' --base '<base from $ARGUMENTS>' --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
```
- Return the command stdout envelopes VERBATIM (code then handoff). Do not
  paraphrase, summarize, or add commentary before or after them. Preserve the
  exit status (0 only when code succeeded and handoff is dual-condition ready;
  1 otherwise, including after a handoff that ran to surface blockers).

Background flow:
- Set `export GROK_COMPANION_EXECUTION_CONTEXT=background` (see
  `plugin/references/execution-context.md`).
- Launch the same command with `Bash(run_in_background: true)`.
- Do not wait for completion or read its output this turn.
- Tell the user: "Grok implement run started in the background. Run
  `/grok:status --run-id <id>` or `/grok:result` when it finishes."

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.

## Hardened only + no auto-apply

`implement` requires **hardened** run-mode. Direct mode is refused fail-closed
(no handoff artifacts exist without isolation evidence). Parent apply stays
manual after a ready handoff - never auto-apply, commit, or push from
`implement`. For apply-on-verified-ready, use `/grok:code --integration auto`
instead. See `skills/handoff/SKILL.md` and `references/implementation-handoff.md`.
