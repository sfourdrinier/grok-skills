---
name: "reason"
description: "Get a cold, isolated Grok second opinion on named artifacts (architecture, debugging, plan critique)"
argument-hint: "(--task <text> | --task-file <path>) [--input <path> ...] [--rules-file <path> ...] [--web] [--schema <path>] [--model <id>] [--timeout <s>] [--max-turns <n>]"
allowed-tools: "Bash(node:*)"
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

<!-- plugin/skills/reason.md -->

Run a Grok `reason` consultation through the hardened wrapper and relay its
result envelope. `reason` works in a fresh private temp dir OUTSIDE the repo
with no automatic rule discovery: only the files you name with `--input` or
`--rules-file` are supplied. It is the cheapest, most deterministic mode -
prefer it over `review` whenever the task does not depend on neighboring repo
files or repo-wide rules.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- Exactly one of `--task <text>` or `--task-file <path>` is required.
- `--input <path>` and `--rules-file <path>` may each be repeated to name the
  artifacts and rule files Grok should see.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.
- Shell-injection safety for `--task <text>`: the task is free text you must NEVER
  place in a shell-evaluated position. `$(...)`/backticks inside a double-quoted
  `--task "..."` run locally BEFORE the wrapper validates them. When the arguments
  carry a `--task <text>`, deliver that text on STDIN with `--task-file -` and a
  SINGLE-QUOTED heredoc so the shell passes it byte-for-byte; the companion stages
  it into a temp file for the wrapper.
- Shell-injection safety for flag VALUES (each `--input <path>`, each `--rules-file
  <path>`, a `--task-file <path>`, `--schema`, `--model`, `--timeout`,
  `--max-turns`, and EVERY other value you substitute from `$ARGUMENTS`): wrap each
  substituted value in SINGLE quotes, for example `--input '<path>'`. Single quotes
  stop the shell from evaluating `$(...)`/backticks, so a hostile value reaches the
  companion as one literal argv token and the wrapper validates it (input/rules
  path resolution + escape guards). An unquoted OR double-quoted value would be
  command-substituted locally BEFORE the wrapper ever sees it -- the same injection
  class as an unsafe `--task "..."`. The bare `--web` flag carries no value to
  quote.

`--web` passthrough:
- Web tools are OFF by default. Pass `--web` only when the reasoning genuinely
  depends on current external practices, current library or software versions,
  or living external documentation the named inputs cannot answer. Do not add
  `--web` otherwise.

Run it as one Bash call and relay the result. When the arguments carry a
`--task <text>`, route that text through STDIN so it is never shell-evaluated:
```bash
node "$SKILL_BASE/run.mjs" reason [--input '<path from $ARGUMENTS>' ...] [--rules-file '<path from $ARGUMENTS>' ...] [other non-task flags from $ARGUMENTS, each substituted value single-quoted] --task-file - <<'GROK_TASK'
<the --task text from $ARGUMENTS, verbatim>
GROK_TASK
```
When the arguments already use `--task-file <path>` (or only non-task flags),
drop the heredoc and pass every flag as single-quoted argv tokens:
```bash
node "$SKILL_BASE/run.mjs" reason [--input '<path from $ARGUMENTS>' ...] [--rules-file '<path from $ARGUMENTS>' ...] --task-file '<path from $ARGUMENTS>' [other non-task flags from $ARGUMENTS, each substituted value single-quoted]
```
- Return the command stdout envelope VERBATIM. Do not paraphrase, summarize, or
  add commentary before or after it. Preserve the exit status. If you want to
  add your own take, do it separately and clearly labeled, AFTER the raw
  envelope.

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
