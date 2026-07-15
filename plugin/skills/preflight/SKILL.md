---
name: "preflight"
description: "Check Grok wrapper readiness (binary, version pin, auth, sandbox, state) - no task is run"
argument-hint: ""
disable-model-invocation: "true"
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
   automatically — do not invent alternate paths.
3. Use a **shell / terminal / Bash tool** to execute the documented command.
   - Claude Code: `Bash` tool (and `AskUserQuestion` when this skill asks for
     wait-vs-background).
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout JSON envelope VERBATIM**. Progress may stream
   on stderr; do not mix it into the envelope.
5. Never put free-text tasks in `--task "..."` (shell injection). Always use
   `--task-file -` with a single-quoted heredoc, or an existing `--task-file` path.


<!-- plugin/skills/preflight.md -->

!`node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" preflight`

The block above ran the wrapper's read-only readiness check. It printed exactly
one JSON result envelope.

Return that stdout envelope to the user VERBATIM. Do not paraphrase, summarize,
reformat, or add commentary on top of it, and preserve its exit status. If you
want to explain it in your own words, do that separately and clearly labeled as
your own summary, AFTER relaying the raw envelope.

If the companion printed an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
