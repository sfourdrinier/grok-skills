---
name: "review"
description: "Run a full-context, read-only Grok code review over a workspace path"
argument-hint: "[--target <path>] [--base <ref>] [--isolated] (--task <text> | --task-file <path>) [--web] [--schema <path>] [--model <id>] [--timeout <s>] [--max-turns <n>] [--wait|--background]"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool
   (the folder that contains this skill's `SKILL.md` and `run.mjs`).
2. Set `SKILL_BASE` to that path. Do **not** invent versioned cache paths.
3. Always invoke the companion **only** through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
node "$SKILL_BASE/run.mjs" <mode> [args...]
```

`run.mjs` finds the plugin install from its own location and runs
`scripts/grok-companion.mjs`. No `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` required.

If the host already exported `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT`, you may call
`node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs"` instead; prefer
`"$SKILL_BASE/run.mjs"` whenever the Skill tool loaded this skill.

Return companion **stdout verbatim**. Never put free-text in `--task "..."`;
use `--task-file -` with a single-quoted heredoc.

<!-- plugin/skills/review.md -->

Run a Grok `review` through the hardened wrapper and relay its result envelope.

Raw slash-command arguments:
`$ARGUMENTS`

Core constraint:
- This command is review-only. Do not fix issues, apply patches, or suggest you
  are about to make changes. Your only job is to run the review and return the
  wrapper's stdout envelope VERBATIM.

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--target` defaults to `.` (repo root / working tree) when omitted.
- `--base <ref>` frames a branch review against that base (comparison text
  only). It does **not** force worktree isolation.
- `--isolated` (opt-in) runs the review in an owned external worktree at HEAD
  with tracked dirty applied. Use when you need a clean snapshot without live
  checkout noise. Setup failures fail closed as `isolation-unavailable` (no
  silent fallback to the live tree). Default is **live checkout**.
- Exactly one of `--task <text>` or `--task-file <path>` is required. Prefer
  `--task-file` for anything beyond a short one-line prompt.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.
- Shell-injection safety for `--task <text>`: the task is free text you must NEVER
  place in a shell-evaluated position. `$(...)`/backticks inside a double-quoted
  `--task "..."` run locally BEFORE the wrapper validates them. When the arguments
  carry a `--task <text>`, deliver that text on STDIN with `--task-file -` and a
  SINGLE-QUOTED heredoc so the shell passes it byte-for-byte; the companion stages
  it into a temp file for the wrapper.
- Shell-injection safety for flag VALUES (`--target`, a `--task-file <path>`,
  `--schema`, `--model`, `--timeout`, `--max-turns`, and EVERY other value you
  substitute from `$ARGUMENTS`): wrap each substituted value in SINGLE quotes, for
  example `--target '<path>'`. Single quotes stop the shell from evaluating
  `$(...)`/backticks, so a hostile value reaches the companion as one literal argv
  token and the wrapper validates it (target path resolution + escape guards). An
  unquoted OR double-quoted value would be command-substituted locally BEFORE the
  wrapper ever sees it -- the same injection class as an unsafe `--task "..."`.
  The bare `--web` flag carries no value to quote.

`--web` passthrough:
- Web tools are OFF by default (review determinism). Pass `--web` only when the
  review genuinely depends on current external practices, current library or
  software versions, or living external documentation the repo's own rules and
  code cannot answer (for example "does this match the latest stable API for
  library X"). Do not add `--web` otherwise.

Execution mode (foreground vs background):
- If the raw arguments include `--wait`, run in the foreground (do not ask).
- If the raw arguments include `--background`, run in a Claude background task
  (do not ask).
- `--wait` and `--background` are Claude Code execution flags. Do NOT forward
  them to the companion or the wrapper; strip them from the wrapper argv.
- Otherwise, estimate the review size first:
  - Run `git status --short --untracked-files=all`.
  - Run `git diff --shortstat` for the working tree.
  - Recommend foreground only when the change is clearly tiny (roughly 1-2
    files). In every other case, including unclear size, recommend background.
- Then use `AskUserQuestion` exactly once, with the recommended option first and
  its label suffixed with ` (Recommended)`. The two options are:
  - `Wait for results`
  - `Run in background`

Foreground flow (one Bash call, then relay verbatim). When the arguments carry a
`--task <text>`, route that text through STDIN so it is never shell-evaluated:
```bash
node "$SKILL_BASE/run.mjs" review --target '<target from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>`, drop the heredoc and pass
every flag as single-quoted argv tokens:
```bash
node "$SKILL_BASE/run.mjs" review --target '<target from $ARGUMENTS>' --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
```
- Return the command stdout envelope VERBATIM. Do not paraphrase, summarize, or
  add commentary before or after it. Preserve the exit status. Do not fix any
  issue the review reports.

Background flow:
- Launch the same command with `Bash(run_in_background: true)`.
- Do not wait for completion or read its output this turn.
- Tell the user: "Grok review started in the background. Run `/grok:status
  --run-id <id>` to read the result envelope (the run id is printed when the run
  finishes)."

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
