---
name: "cleanup"
description: "Report (dry-run) or remove a Grok run's owned session state and worktree by run id"
argument-hint: "--run-id <run-id> [--confirm]"
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
   automatically. **Never invent cache paths** under `~/.claude/plugins/cache` or
   `~/.codex/plugins/cache` - only use `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` from the
   host (see `plugin/references/plugin-root.md`).
3. Use a **shell / terminal / Bash tool** to execute the documented command.
   - Claude Code: `Bash` tool (and `AskUserQuestion` when this skill asks for
     wait-vs-background).
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout JSON envelope VERBATIM**. Progress may stream
   on stderr; do not mix it into the envelope.
5. Never put free-text tasks in `--task "..."` (shell injection). Always use
   `--task-file -` with a single-quoted heredoc, or an existing `--task-file` path.


<!-- plugin/skills/cleanup.md -->

Report (dry-run) or remove a Grok run's owned session state and worktree by run
id through the hardened wrapper and relay its result envelope. It prints exactly
one JSON result envelope.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--run-id <run-id>` is required. If the user did not supply one, ask for it
  BEFORE running anything.
- `--confirm` is optional and passed through only when the user supplied it.
  - Without `--confirm` it is a DRY RUN: it reports the owned session state and
    (for `code`/`verify`) the worktree and branch it WOULD remove. Nothing is
    deleted.
  - With `--confirm` it actually removes them. Removal is gated on OWNERSHIP,
    not cleanliness: the wrapper refuses (fail closed) when the sibling owner
    marker is missing, unmarked/foreign, or its run id does not match the
    requested `--run-id`, and when the worktree directory name does not match
    the requested `--run-id`. When both DO match the requested run, the worktree
    is removed whether it is clean OR dirty (`code` mode intentionally leaves its
    worktree dirty), so a dirty owner-marked worktree owned by the requested run
    is removed, never refused.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.

Run it as one Bash call and relay the result. SINGLE-QUOTE the run id so it
reaches the companion as one literal argv element; NEVER embed the raw argument
inside a position the shell would evaluate. An unquoted OR double-quoted value
containing `$(...)`/backticks is command-substituted locally BEFORE the wrapper
ever validates it. Single quotes pass the bytes verbatim; the wrapper then
rejects any run id that is not the strict `YYYYMMDDThhmmssZ-xxxxxx` run-id shape
and binds the destructive removal to the requested run id:
```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" cleanup --run-id '<run-id from $ARGUMENTS>' [--confirm]
```
- Return the command stdout envelope to the user VERBATIM. Do not paraphrase,
  summarize, reformat, or add commentary before or after it, and preserve the
  exit status.

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
