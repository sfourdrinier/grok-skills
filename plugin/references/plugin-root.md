# Plugin root and paths (Claude + Codex)

## Never invent cache paths

Do **not** construct paths like:

- `~/.claude/plugins/cache/...`
- `~/.codex/plugins/cache/...`
- version folders (`1.2.0`, `1.2.2`) guessed by hand

Hosts set the install root for you:

| Host | Env |
|------|-----|
| Claude Code | `CLAUDE_PLUGIN_ROOT` (plugin install / cache tree) |
| Codex skills / hooks | `PLUGIN_ROOT` (and often `CLAUDE_PLUGIN_ROOT` for compatibility) |
| Codex **custom agents** | Absolute `GROK_COMPANION=.../grok-companion.mjs` injected into `~/.codex/agents/*.toml` at SessionStart |

## Resolve once

```bash
GROK_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:?plugin root not set}}"
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" …
```

The Python wrapper is always:

`"$GROK_PLUGIN_ROOT/wrapper/scripts/grok_agent.py"`

(resolved by the companion automatically).

## Codex agents uninstall

Managed agents only (`# managed-by: grok-skills`):

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --remove-codex-agents
```

Creates `*.toml.bak` backups. User-edited agents without the managed header are left alone.
While the plugin remains enabled, **SessionStart will reinstall** managed agents; disable or uninstall the plugin first if you want them gone permanently.
