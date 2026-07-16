---
name: "setup"
description: "Check Grok readiness and optionally toggle stop gate / run mode (Codex agents auto-install on SessionStart)"
argument-hint: "[--enable-review-gate | --disable-review-gate] [--run-mode hardened|direct] [--force-codex-agents] [--skip-codex-agents] [--remove-codex-agents]"
allowed-tools: "Bash(node:*)"
---

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool
   (the folder that contains this skill's `SKILL.md` and `run.mjs`).
2. Set `SKILL_BASE` to that path. Do **not** invent versioned cache paths.
3. Always invoke the companion **only** through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
node "$SKILL_BASE/run.mjs" <mode> [args...]
```

`run.mjs` finds the plugin install from its own location and runs
`scripts/grok-companion.mjs`. No `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` required.

If the host already exported `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT`, you may call
`node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs"` instead; prefer
`"$SKILL_BASE/run.mjs"` whenever the Skill tool loaded this skill.

Return companion **stdout verbatim**. Never put free-text in `--task "..."`;
use `--task-file -` with a single-quoted heredoc.

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
node "$SKILL_BASE/run.mjs" setup [flags from "$ARGUMENTS"]
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
node "$SKILL_BASE/run.mjs" setup
node "$SKILL_BASE/run.mjs" setup --run-mode hardened
node "$SKILL_BASE/run.mjs" setup --force-codex-agents
node "$SKILL_BASE/run.mjs" setup --remove-codex-agents
node "$SKILL_BASE/run.mjs" setup --enable-review-gate
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
  agents natively yet - [openai/codex#18988](https://github.com/openai/codex/issues/18988)).
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
