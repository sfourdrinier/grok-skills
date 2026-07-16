# Plugin root resolution (Claude + Codex)

## How OpenAI's Codex-for-Claude plugin does it

That plugin's **user surface is `commands/`**, often with:

- harness bang lines / frontmatter that expand `${CLAUDE_PLUGIN_ROOT}` before shell
- `disable-model-invocation: true` on those commands so the model does not drive Bash without env

Internal skills still mention `${CLAUDE_PLUGIN_ROOT}` and are mainly for plugin agents.

**They avoid the Skill-tool gap** by not relying on model-driven Bash + env-only resolve.

## Why grok-skills needs more

We **enable model invocation** of skills (Codex Skill tool / Claude Skill tool). The
Skill tool injects `SKILL.md` into context; it does **not** put `CLAUDE_PLUGIN_ROOT`
into later Bash tool environments. Env-only resolve then fails with:

```text
PLUGIN_ROOT: plugin root not set
```

## Contract (required)

Resolve **plugin root** (directory that contains `scripts/` and `skills/`) in this order:

| Priority | Source | Notes |
|----------|--------|--------|
| 1 | `CLAUDE_PLUGIN_ROOT` | Claude hooks, some command expansions, plugin agents |
| 2 | `PLUGIN_ROOT` | Codex plugin hooks / dual-host |
| 3 | Skill base directory | Skill tool **"Base directory for this skill"** = absolute `.../skills/<name>` |

After resolve, the companion is always:

```text
$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs
```

Validate that file exists before calling Node.

### Skill-tool path (no env)

1. Copy the absolute **Base directory for this skill** from the Skill tool into `SKILL_DIR`.
2. Either:
   - `GROK_PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"`, or
   - resolve companion:  
     `COMPANION="$(cd "$SKILL_DIR/../.." && pwd)/scripts/grok-companion.mjs"`
3. Or use the helper (same math, validates companion):

```bash
SKILL_DIR='<absolute Base directory for this skill from Skill tool>'
COMPANION="$(node "$SKILL_DIR/../../scripts/resolve-plugin-root.mjs" --skill-dir "$SKILL_DIR" --companion)"
node "$COMPANION" <mode> ...
```

`../../scripts/...` from `skills/<name>` is the **shipped layout**, not a guessed cache version.

### Never invent versioned cache paths

Do **not** construct:

- `~/.claude/plugins/cache/grok-skills/grok/<version>/...` by guessing `<version>`
- `~/.codex/plugins/cache/...` by guessing a revision folder

Do **use**:

- host env (`CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT`), or
- host-provided skill base directory (Skill tool), or
- absolute `GROK_COMPANION` already written into managed Codex agents (`~/.codex/agents/*.toml`)

### Codex custom agents

SessionStart installs managed TOML with an **absolute** `GROK_COMPANION=...` path.
Those agents do not need `PLUGIN_ROOT` at spawn.

### Uninstall managed Codex agents

```bash
# After resolving COMPANION as above:
node "$COMPANION" setup --remove-codex-agents
```

Disable/uninstall the plugin first if you do not want SessionStart to reinstall them.

## Canonical Bash resolve block

Paste at the start of every skill Bash sequence:

```bash
# Prefer host env; else SKILL_DIR = Skill tool "Base directory for this skill" (absolute).
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
# Then: node "$COMPANION" <mode> ...
```

## Resolver library

- `plugin/scripts/lib/resolve-plugin-root.mjs` — pure resolve + validation
- `plugin/scripts/resolve-plugin-root.mjs` — CLI (`--skill-dir`, `--companion`, `--json`)
