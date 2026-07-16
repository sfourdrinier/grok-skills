---
name: "dual-lens"
description: "Run adversarial-review then review on the same target (dual-lens harden recipe)"
argument-hint: "[--target <path>] [--task <text> | --task-file <path>]"
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

## Procedure

Use one completion signal for the recipe: pass `--no-notify` on the first
(adversarial) run so only the final review may notify when notifications are on.

1. Adversarial pass (web on by default; suppress intermediate notify):

```bash
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" adversarial-review --no-notify [flags from "$ARGUMENTS"] --task-file - <<'GROK_TASK'
<operator focus or paste the task>
GROK_TASK
```

2. Normal review on the **same target** (may notify):

```bash
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" review [same --target] --task-file - <<'GROK_TASK'
Confirm or refute high/critical findings from the adversarial pass. Prefer residual risks.
GROK_TASK
```

3. Summarize both envelopes (severity, residual risks). Do not invent a merged score.
   Return each envelope **verbatim** before your summary.

Full recipe: repo `docs/dual-lens-harden.md`.
