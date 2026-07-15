---
name: "debate"
description: "Bounded Grok debate: two opposing reason passes, then synthesis"
argument-hint: "(--task <text> | --task-file <path>)"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Harness compatibility (Claude Code + Codex / ChatGPT)

Resolve plugin root and run the companion with Node:

```bash
GROK_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:?plugin root not set}}"
```
**Never invent cache paths** under `~/.claude/plugins/cache` or `~/.codex/plugins/cache` - only host-exported roots (see `plugin/references/plugin-root.md`).


Use the shell/Bash tool. Return companion stdout verbatim unless the skill says otherwise.
Never put free-text in `--task "..."`; use `--task-file -` + single-quoted heredoc.

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" debate --task-file - <<'GROK_TASK'
<topic or design under debate>
GROK_TASK
```

Return the final envelope (side B / synthesis) verbatim. Mention job ids if printed on stderr.
