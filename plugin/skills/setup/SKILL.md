---
name: "setup"
description: "Check Grok readiness and optionally toggle stop gate / run mode (Codex agents auto-install on SessionStart)"
argument-hint: "[--enable-review-gate | --disable-review-gate] [--run-mode hardened|direct] [--force-codex-agents] [--skip-codex-agents] [--remove-codex-agents]"
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
   - Claude Code: `Bash` tool.
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout** (setup report / envelopes) as the user-facing result.
5. Never put free-text tasks in `--task "..."` (shell injection).

<!-- plugin/skills/setup/SKILL.md -->

`/grok:setup` (or Codex skill `setup`) is **optional**. It reports readiness and can
toggle the stop gate / run mode.

**Codex agents install automatically** on `SessionStart` (hook writes managed
TOML under `~/.codex/agents/` with an absolute path to `grok-companion.mjs`).
You should not need a manual setup step after installing the plugin.

Raw arguments:
`$ARGUMENTS`

## Primary command

Forward optional flags from `$ARGUMENTS` (each value single-quoted if present):

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup [flags from "$ARGUMENTS"]
```

Supported flags:

| Flag | Effect |
|------|--------|
| `--run-mode hardened` | Persist hardened mode (default) |
| `--run-mode direct` | Persist direct (installed Grok CLI home) |
| `--enable-review-gate` | Opt-in stop-time review gate |
| `--disable-review-gate` | Turn gate off |
| `--force-codex-agents` | Overwrite user-owned `~/.codex/agents/grok-*.toml` (writes `*.bak` first) |
| `--skip-codex-agents` | Skip agent ensure for this run only |
| `--remove-codex-agents` | Remove **managed** agents only (backups as `*.toml.bak`); user-owned kept |

Examples:

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --run-mode hardened
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --force-codex-agents
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --remove-codex-agents
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --enable-review-gate
```

## What the report includes

- Grok CLI presence / version
- Bundled wrapper path
- Run mode (hardened vs direct)
- Stop-review gate on/off
- **Codex agents** ensure result (dest `~/.codex/agents/`, absolute companion)
- Hardened preflight checks when wrapper is available

## Agents (zero post-install)

| Agent | Host | Role |
|-------|------|------|
| `grok-engineer-coder` | Claude (`plugin/agents/`) + Codex (`~/.codex/agents/`) | Grok implements code in an isolated worktree; host orchestrates |
| `grok-rescue` | Claude + Codex | Diagnosis / second opinion via Grok `reason` (or `code` if target+base given) |

- **Claude Code:** loads `plugin/agents/` from the install automatically.
- **Codex:** SessionStart auto-installs managed agents (Codex cannot register plugin
  agents natively yet — [openai/codex#18988](https://github.com/openai/codex/issues/18988)).
  Managed files refresh when the plugin cache path or templates change (with
  `*.bak` backup). User-edited files without the `managed-by: grok-skills`
  header are left alone unless `--force-codex-agents`.
- **Uninstall managed Codex agents:** disable/uninstall the plugin first (or they
  reappear on SessionStart), then
  `setup --remove-codex-agents` (or delete `~/.codex/agents/grok-*.toml` that have
  the managed header). See `plugin/references/plugin-root.md`.

## Gate behavior (if enabled)

When the stop-review gate is ON, ending a turn runs a structured Grok review and
**blocks** on critical/high findings, missing structured findings, or setup/auth
failures. Free-text "success" alone does not end the session. Codex may require
hook trust via `/hooks`.
