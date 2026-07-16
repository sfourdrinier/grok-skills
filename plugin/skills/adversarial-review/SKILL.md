---
name: "adversarial-review"
description: "Hostile Grok review that challenges design; web on by default"
argument-hint: "[--target <path>] [--task <text> | --task-file <path>] [--web|--no-web]"
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

## Run

Never `--task "..."`. Example:

```bash
node "$COMPANION" adversarial-review [flags from $ARGUMENTS, single-quoted] --task-file - <<'GROK_TASK'
<operator focus>
GROK_TASK
```

Return the companion stdout envelope verbatim.
