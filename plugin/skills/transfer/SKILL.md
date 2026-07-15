---
name: "transfer"
description: "Package the current Claude session into a Grok task pack"
argument-hint: "[--source <claude-jsonl>]"
disable-model-invocation: "true"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Harness compatibility (Claude Code + Codex / ChatGPT)

Resolve plugin root and run the companion with Node:

```bash
GROK_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:?plugin root not set}}"
```

Use the shell/Bash tool. Return companion stdout verbatim unless the skill says otherwise.
Never put free-text in `--task "..."`; use `--task-file -` + single-quoted heredoc.

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" transfer "$ARGUMENTS"
```

Show the transfer pack paths and suggested follow-up commands exactly.
