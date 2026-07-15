---
name: "code"
description: "Have Grok implement code in an isolated external worktree (nothing is committed or pushed)"
argument-hint: "--target <path> --base <revision> (--task <text> | --task-file <path>) [--web] [--model <id>] [--timeout <s>] [--max-turns <n>]"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Harness compatibility (Claude Code + Codex / ChatGPT)

This skill works in **Claude Code** and **Codex** (CLI + ChatGPT desktop).

1. Resolve the plugin root (both harnesses export one of these):
```bash
GROK_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:?plugin root not set}}"
```
2. Run the companion with **Node** (required). The hardened Python wrapper is
   bundled at `"$GROK_PLUGIN_ROOT/wrapper/scripts/grok_agent.py"` and is resolved
   automatically. **Never invent cache paths** under `~/.claude/plugins/cache` or
   `~/.codex/plugins/cache` - only use `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` from the
   host (see `plugin/references/plugin-root.md`).
3. Use a **shell / terminal / Bash tool** to execute the documented command.
   - Claude Code: `Bash` tool (and `AskUserQuestion` when this skill asks for
     wait-vs-background).
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout JSON envelope VERBATIM**. Progress may stream
   on stderr; do not mix it into the envelope.
5. Never put free-text tasks in `--task "..."` (shell injection). Always use
   `--task-file -` with a single-quoted heredoc, or an existing `--task-file` path.


<!-- plugin/skills/code.md -->

Run a Grok `code` implementation through the hardened wrapper and relay its
result envelope. The wrapper creates and verifies its own external git worktree
(never the current checkout), runs the workspace build gate, and keeps the
worktree for inspection. Nothing is ever committed, merged, pushed, or deleted
automatically.

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
- Shell-injection safety for `--task <text>`: the task is free text you must NEVER
  place in a shell-evaluated position. `$(...)`/backticks inside a double-quoted
  `--task "..."` run locally BEFORE the wrapper validates them. When the arguments
  carry a `--task <text>`, deliver that text on STDIN with `--task-file -` and a
  SINGLE-QUOTED heredoc so the shell passes it byte-for-byte; the companion stages
  it into a temp file for the wrapper.
- Shell-injection safety for flag VALUES (`--target`, `--base`, a `--task-file
  <path>`, `--model`, `--timeout`, `--max-turns`, and EVERY other value you
  substitute from `$ARGUMENTS`): wrap each substituted value in SINGLE quotes, for
  example `--target '<path>' --base '<revision>'`. Single quotes stop the shell
  from evaluating `$(...)`/backticks, so a hostile value reaches the companion as
  one literal argv token and the wrapper validates it (target/worktree path
  resolution + escape guards). An unquoted OR double-quoted value would be
  command-substituted locally BEFORE the wrapper ever sees it -- the same
  injection class as an unsafe `--task "..."`. The bare `--web` flag carries no
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
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" code --target '<target from $ARGUMENTS>' --base '<base from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>`, drop the heredoc and pass
every flag as single-quoted argv tokens:
```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" code --target '<target from $ARGUMENTS>' --base '<base from $ARGUMENTS>' --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
```
- Return the command stdout envelope VERBATIM. Do not paraphrase, summarize, or
  add commentary before or after it. Preserve the exit status.

Background flow:
- Launch the same command with `Bash(run_in_background: true)`.
- Do not wait for completion or read its output this turn.
- Tell the user: "Grok code run started in the background. Run `/grok:status
  --run-id <id>` to read the result envelope."

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
