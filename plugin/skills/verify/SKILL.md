---
name: "verify"
description: "Have Grok independently verify a change in an existing worktree (no source edits, hermetic)"
argument-hint: "--worktree <absolute-path> (--task <text> | --task-file <path>) [--model <id>] [--timeout <s>] [--max-turns <n>]"
allowed-tools: "Bash(node:*)"
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


<!-- plugin/skills/verify.md -->

Run a Grok `verify` through the hardened wrapper and relay its result envelope.
`verify` inspects and tests a change in an EXISTING worktree (typically one a
prior `code` run produced), has no source-editing tools, and always ends with a
machine-readable `pass` / `fail` / `inconclusive` verdict plus evidence.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--worktree <absolute-path-to-worktree>` is required.
- Exactly one of `--task <text>` or `--task-file <path>` is required.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.
- Shell-injection safety for `--task <text>`: the task is free text you must NEVER
  place in a shell-evaluated position. `$(...)`/backticks inside a double-quoted
  `--task "..."` run locally BEFORE the wrapper validates them. When the arguments
  carry a `--task <text>`, deliver that text on STDIN with `--task-file -` and a
  SINGLE-QUOTED heredoc so the shell passes it byte-for-byte; the companion stages
  it into a temp file for the wrapper.
- Shell-injection safety for flag VALUES (`--worktree`, a `--task-file <path>`,
  `--model`, `--timeout`, `--max-turns`, and EVERY other value you substitute from
  `$ARGUMENTS`): wrap each substituted value in SINGLE quotes, for example
  `--worktree '<absolute-path>'`. Single quotes stop the shell from evaluating
  `$(...)`/backticks, so a hostile value reaches the companion as one literal argv
  token and the wrapper validates it (worktree path resolution + escape guards).
  An unquoted OR double-quoted value would be command-substituted locally BEFORE
  the wrapper ever sees it -- the same injection class as an unsafe `--task "..."`.

NO web access:
- `verify` NEVER accepts `--web` - independent verification stays hermetic by
  design. If the user asks for `--web` on a verify run, do NOT add it; explain
  that verify is hermetic and offer `reason --web` or `review --web` instead.

Run it as one Bash call and relay the result. When the arguments carry a
`--task <text>`, route that text through STDIN so it is never shell-evaluated:
```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" verify --worktree '<worktree from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>`, drop the heredoc and pass
every flag as single-quoted argv tokens:
```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" verify --worktree '<worktree from $ARGUMENTS>' --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
```
- Return the command stdout envelope VERBATIM. Do not paraphrase, summarize, or
  add commentary before or after it. Preserve the exit status. The verdict lives
  in the envelope's `verifier.verdict`; do not restate it as your own conclusion
  in place of the raw envelope.

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
