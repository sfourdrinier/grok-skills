---
name: "review"
description: "Run a full-context, read-only Grok code review over a workspace path"
argument-hint: "[--target <path>] [--base <ref>] (--task <text> | --task-file <path>) [--web] [--schema <path>] [--model <id>] [--timeout <s>] [--max-turns <n>] [--wait|--background]"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Resolve plugin root (required)

Host env is set for hooks/commands, **not** for Bash after a Skill-tool load.
Use env when present; otherwise set `SKILL_DIR` to the absolute **Base directory
for this skill** from the Skill tool (ends with `skills/<name>`).

See `plugin/references/plugin-root.md`. Do **not** invent versioned cache paths.

```bash
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  GROK_PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT"
elif [ -n "${PLUGIN_ROOT:-}" ]; then
  GROK_PLUGIN_ROOT="$PLUGIN_ROOT"
elif [ -n "${SKILL_DIR:-}" ]; then
  GROK_PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
else
  echo "plugin root not set: set CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT or SKILL_DIR (Skill tool base directory)" >&2
  exit 127
fi
COMPANION="$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs"
if [ ! -f "$COMPANION" ]; then
  echo "companion not found at $COMPANION (invalid plugin root)" >&2
  exit 127
fi
```

Then run: `node "$COMPANION" ...` (not a bare env-only root line).

## Harness compatibility (Claude Code + Codex / ChatGPT)

This skill works in **Claude Code** and **Codex** (CLI + ChatGPT desktop).

1. Resolve plugin root with the **Resolve plugin root** section above (env or `SKILL_DIR`).
2. Run the companion with **Node**: `node "$COMPANION" ...`. The hardened Python
   wrapper is under `$GROK_PLUGIN_ROOT/wrapper/scripts/grok_agent.py` and is
   resolved by the companion automatically.
3. Use a **shell / terminal / Bash tool** to execute the documented command.
   - Claude Code: `Bash` tool (and `AskUserQuestion` when this skill asks for
     wait-vs-background).
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout JSON envelope VERBATIM**. Progress may stream
   on stderr; do not mix it into the envelope.
5. Never put free-text tasks in `--task "..."` (shell injection). Always use
   `--task-file -` with a single-quoted heredoc, or an existing `--task-file` path.


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
- `--base <ref>` frames a branch review against that base.
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
node "$COMPANION" review --target '<target from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>`, drop the heredoc and pass
every flag as single-quoted argv tokens:
```bash
node "$COMPANION" review --target '<target from $ARGUMENTS>' --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
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
