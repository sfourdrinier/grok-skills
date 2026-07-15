---
name: "adversarial-review"
description: "Adversarial Grok review that challenges design and hunts failure modes"
argument-hint: "[--wait|--background] [--base <ref>] [--target <path>] [--web] [focus text]"
disable-model-invocation: "true"
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

Run an adversarial Grok review through the companion (maps to hardened review
with adversarial framing; web search on by default).

Raw arguments: `$ARGUMENTS`

Rules:
- Review only. Do not fix code.
- Strip `--wait` / `--background` from companion argv (Claude execution flags).
- If neither wait nor background: estimate size via git shortstat; recommend background unless tiny; AskUserQuestion once.
- Pass remaining flags single-quoted. Free-text focus after flags goes on stdin via `--task-file -`.

Foreground:

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" adversarial-review [flags from $ARGUMENTS, single-quoted] --task-file - <<'GROK_TASK'
<focus text from $ARGUMENTS, or empty>
GROK_TASK
```

Return stdout envelope verbatim.
