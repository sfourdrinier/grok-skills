---
name: "dual-lens"
description: "Run adversarial-review then review on the same target (dual-lens harden recipe)"
argument-hint: "[--target <path>] [--task <text> | --task-file <path>]"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Harness compatibility (Claude Code + Codex / ChatGPT)

Resolve plugin root and run the companion with Node:

```bash
GROK_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:?plugin root not set}}"
```
**Never invent cache paths** under `~/.claude/plugins/cache` or `~/.codex/plugins/cache` - only host-exported roots (see `plugin/references/plugin-root.md`).


Use the shell/Bash tool. Return companion stdout verbatim for each pass.
Never put free-text in `--task "..."`; use `--task-file -` + single-quoted heredoc.

## Procedure

1. Run the adversarial pass (web on by default):

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" adversarial-review [flags from "$ARGUMENTS"] --task-file - <<'GROK_TASK'
<operator focus or paste the task>
GROK_TASK
```

2. Run a normal review on the **same target** that confirms or refutes high/critical attacks:

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" review [same --target] --task-file - <<'GROK_TASK'
Confirm or refute high/critical findings from the adversarial pass. Prefer residual risks.
GROK_TASK
```

3. Summarize both envelopes for the operator: severity list, residual risks, and any
   `grounding-requested-no-sources` warning. Do not invent a merged "score".

Full recipe notes: repo `docs/dual-lens-harden.md`.
