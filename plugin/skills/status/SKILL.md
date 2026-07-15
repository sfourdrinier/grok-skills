---
name: "status"
description: "Read back a prior Grok run's stored envelope and progress by run id (read-only)"
argument-hint: "[--run-id <run-id>]"
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


<!-- plugin/skills/status.md -->

Read back a prior Grok run's stored envelope and progress by run id through the
hardened wrapper and relay its result envelope. `status` is strictly read-only:
it prints exactly one JSON result envelope and never writes to the run it
inspects.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--run-id <run-id>` is required. If the user did not supply one, ask them for
  the run id (it is printed in every run's envelope as `runId`) BEFORE running
  anything.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.

Run it as one Bash call and relay the result. SINGLE-QUOTE the run id so it
reaches the companion as one literal argv element; NEVER embed the raw argument
inside a position the shell would evaluate. An unquoted OR double-quoted value
containing `$(...)`/backticks is command-substituted locally BEFORE the wrapper
ever validates it. Single quotes pass the bytes verbatim; the wrapper then
rejects any run id that is not the strict `YYYYMMDDThhmmssZ-xxxxxx` run-id shape:
```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" status --run-id '<run-id from $ARGUMENTS>'
```
- Return the command stdout envelope to the user VERBATIM. Do not paraphrase,
  summarize, reformat, or add commentary before or after it, and preserve the
  exit status.

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.


Without `--run-id`, the companion prints the local **jobs table** for this workspace
(recent companion-tracked runs). With `--run-id`, it returns the wrapper status envelope.

Also: `/grok:jobs`, `/grok:result`, `/grok:cancel`.
