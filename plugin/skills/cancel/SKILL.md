---
name: "cancel"
description: "Cancel a running Grok job"
argument-hint: "[job-id]"
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
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" cancel "$ARGUMENTS"
```

Present the cancel confirmation as returned.
