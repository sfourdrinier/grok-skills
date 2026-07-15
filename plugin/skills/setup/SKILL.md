---
name: "setup"
description: "Check Grok readiness, toggle stop gate / run mode, and install Codex agents (engineer-coder + rescue)"
argument-hint: "[--enable-review-gate | --disable-review-gate] [--run-mode hardened|direct] [--force-codex-agents] [--skip-codex-agents]"
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
   - Claude Code: `Bash` tool.
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout** (setup report / envelopes) as the user-facing result.
5. Never put free-text tasks in `--task "..."` (shell injection).

<!-- plugin/skills/setup/SKILL.md -->

`/grok:setup` (or Codex skill `setup`) runs readiness reporting, optional gate/mode
toggles, and **installs Codex custom agents** into `~/.codex/agents/` by default
(`grok-engineer-coder`, `grok-rescue`).

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
| `--force-codex-agents` | Overwrite existing `~/.codex/agents/grok-*.toml` |
| `--skip-codex-agents` | Do not install Codex agent templates |

Examples:

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --run-mode hardened
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --force-codex-agents
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --enable-review-gate
```

## What the report includes

- Grok CLI presence / version
- Bundled wrapper path
- Run mode (hardened vs direct)
- Stop-review gate on/off
- **Codex agents** install result (dest `~/.codex/agents/`)
- Hardened preflight checks when wrapper is available

## Agents after setup

| Agent | Host | Role |
|-------|------|------|
| `grok-engineer-coder` | Claude (`plugin/agents/`) + Codex (`~/.codex/agents/`) | Grok implements code in an isolated worktree; host orchestrates |
| `grok-rescue` | Claude + Codex | Diagnosis / second opinion via Grok `reason` (or `code` if target+base given) |

Claude Code loads plugin agents from the install automatically after
`/reload-plugins`. Codex needs the TOML files under `~/.codex/agents/` (this
setup step installs them).

## Gate behavior (if enabled)

When the stop-review gate is ON, ending a turn runs a structured Grok review and
**blocks** on critical/high findings, missing structured findings, or setup/auth
failures. Free-text "success" alone does not end the session. Codex may require
hook trust via `/hooks`.
