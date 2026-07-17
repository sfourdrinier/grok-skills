---
name: "setup"
description: "Check Grok readiness and optionally toggle stop gate / run mode / Codex agents scope (Codex agents auto-install on SessionStart)"
argument-hint: "[--enable-review-gate | --disable-review-gate] [--run-mode hardened|direct] [--integration direct|worktree|auto|review] [--notification-mode off|auto|native|webhook] [--notification-webhook-url <url>] [--codex-agents-scope user|project] [--force-codex-agents] [--skip-codex-agents] [--remove-codex-agents]"
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
toggle the stop gate / run mode / Codex agents install scope.

**Codex agents install automatically** on `SessionStart` (hook writes managed
TOML with absolute `GROK_AGENT_RUN` → `agents/run.mjs`). Default dest is personal
`~/.codex/agents/`; `setup --codex-agents-scope project` persists workspace prefs
and installs into `<cwd>/.codex/agents/` instead (SessionStart honors the same
prefs). You should not need a manual setup step after installing the plugin.

**Codex trust honesty:** on Codex, plugin hooks (the optional stop-review gate
**and** the SubagentStop handoff nudge) stay **dormant until you trust them** via
`/hooks`. That is the honest default posture - install alone does not enable those
hooks. Skills and SessionStart agent materialization still work without hook trust.

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
| `--integration direct` | Persist integration mode **and** record one-time consent for live-tree edits (orthogonal to run mode) |
| `--integration worktree\|auto\|review` | Persist integration mode (no consent required; isolated / review paths) |
| `--notification-mode off\|auto\|native\|webhook` | Completion signal prefs (default `off`; **auto** recommended for background jobs) |
| `--notification-webhook-url <url>` | Webhook URL when mode is `webhook` |
| `--enable-review-gate` | Opt-in stop-time review gate |
| `--disable-review-gate` | Turn gate off |
| `--codex-agents-scope user\|project` | Persist install scope (default `user` = `~/.codex/agents/`; `project` = `<cwd>/.codex/agents/`). SessionStart honors the same prefs. |
| `--force-codex-agents` | Overwrite user-owned `grok-*.toml` in the active scope dir (writes `*.bak` first; managed backups capped at 3) |
| `--skip-codex-agents` | Skip agent ensure for this run only |
| `--remove-codex-agents` | Remove **managed** agents only (backups as `*.toml.bak`); user-owned kept |

Examples:

```bash
node "$SKILL_BASE/run.mjs" setup
node "$SKILL_BASE/run.mjs" setup --run-mode hardened
node "$SKILL_BASE/run.mjs" setup --integration direct
node "$SKILL_BASE/run.mjs" setup --notification-mode auto
node "$SKILL_BASE/run.mjs" setup --codex-agents-scope project
node "$SKILL_BASE/run.mjs" setup --force-codex-agents
node "$SKILL_BASE/run.mjs" setup --remove-codex-agents
node "$SKILL_BASE/run.mjs" setup --enable-review-gate
```

## What the report includes

- Grok CLI presence / version
- Bundled wrapper path
- Run mode (hardened vs direct security posture)
- Integration mode (how code edits land) + direct consent status
- Stop-review gate on/off
- **Codex agents scope** (`user` or `project`)
- **Codex agents** ensure result (dest from scope, absolute `agents/run.mjs`)
- Hardened preflight checks when wrapper is available

## Agents (zero post-install)

| Agent | Host | Role |
|-------|------|------|
| `grok-engineer-coder` (nickname **Grok Coder**) | Claude (`plugin/agents/`) + Codex (`~/.codex/agents/` or project `.codex/agents/`) | Grok implements code in an isolated worktree; host orchestrates |
| `grok-rescue` (nickname **Grok Rescue**) | Claude + Codex | Diagnosis / second opinion via Grok `reason` (or `code` if target+base given) |

- **Claude Code:** loads `plugin/agents/` from the install automatically.
- **Codex:** SessionStart auto-installs managed agents (Codex cannot register plugin
  agents natively yet - [openai/codex#18988](https://github.com/openai/codex/issues/18988)).
  Scope defaults to personal `~/.codex/agents/`; `--codex-agents-scope project`
  installs into `<cwd>/.codex/agents/` (prefs honored on SessionStart; see
  [Codex subagents](https://developers.openai.com/codex/subagents)).
  Managed files refresh when the plugin cache path or templates change (managed
  `*.bak*` capped at 3 newest). User-edited files without the
  `managed-by: grok-skills` header are left alone unless `--force-codex-agents`.
- **Codex hooks dormant until trusted:** stop-review gate and SubagentStop handoff
  nudge stay off until you approve them via `/hooks`. Agent materialization does
  not require that step.
- **Uninstall managed Codex agents:** disable/uninstall the plugin first (or they
  reappear on SessionStart), then
  `setup --remove-codex-agents` (or delete managed `grok-*.toml` under the active
  scope dir). See `plugin/references/plugin-root.md`.

## Gate behavior (if enabled)

When the stop-review gate is ON, ending a turn runs a structured Grok review and
**blocks** on critical/high findings, missing structured findings, or setup/auth
failures. Free-text "success" alone does not end the session. On Codex, plugin
hooks (stop gate **and** SubagentStop nudge) stay **dormant until trusted** via
`/hooks` - that is the honest default posture.
