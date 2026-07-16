---
name: "setup"
description: "Check Grok readiness and optionally toggle stop gate / run mode (Codex agents auto-install on SessionStart)"
argument-hint: "[--enable-review-gate | --disable-review-gate] [--run-mode hardened|direct] [--force-codex-agents] [--skip-codex-agents] [--remove-codex-agents]"
allowed-tools: "Bash(node:*)"
---

## Resolve plugin root (required)

Host env is set for hooks/commands, **not** for Bash after a Skill-tool load.
Use env when present; otherwise set `SKILL_DIR` to the absolute **Base directory
for this skill** from the Skill tool (ends with `skills/<name>`).

See `plugin/references/plugin-root.md`. Do **not** invent versioned cache paths.

```bash
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  GROK_PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT"
elif [ -n "${PLUGIN_ROOT:-}" ]; then
  GROK_PLUGIN_ROOT="$PLUGIN_ROOT"
elif [ -n "${SKILL_DIR:-}" ]; then
  GROK_PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
else
  echo "plugin root not set: set CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT or SKILL_DIR (Skill tool base directory)" >&2
  exit 127
fi
COMPANION="$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs"
if [ ! -f "$COMPANION" ]; then
  echo "companion not found at $COMPANION (invalid plugin root)" >&2
  exit 127
fi
```

Then run: `node "$COMPANION" ...` (not a bare env-only root line).

## Harness compatibility (Claude Code + Codex / ChatGPT)

This skill works in **Claude Code** and **Codex** (CLI + ChatGPT desktop).

1. Resolve plugin root with the **Resolve plugin root** section above (env or `SKILL_DIR`).
2. Run the companion with **Node**: `node "$COMPANION" ...`. The hardened Python
   wrapper is under `$GROK_PLUGIN_ROOT/wrapper/scripts/grok_agent.py` and is
   resolved by the companion automatically.
3. Use a **shell / terminal / Bash tool** to execute the documented command.
   - Claude Code: `Bash` tool (and `AskUserQuestion` when this skill asks for
     wait-vs-background).
   - Codex / ChatGPT: the shell tool (`Bash` / terminal). If no structured
     question UI exists, ask the user in chat and then run foreground.
4. Return the companion **stdout JSON envelope VERBATIM**. Progress may stream
   on stderr; do not mix it into the envelope.
5. Never put free-text tasks in `--task "..."` (shell injection). Always use
   `--task-file -` with a single-quoted heredoc, or an existing `--task-file` path.


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
node "$COMPANION" setup [flags from "$ARGUMENTS"]
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
node "$COMPANION" setup
node "$COMPANION" setup --run-mode hardened
node "$COMPANION" setup --force-codex-agents
node "$COMPANION" setup --remove-codex-agents
node "$COMPANION" setup --enable-review-gate
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
