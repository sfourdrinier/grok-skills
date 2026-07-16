# Plugin root and skill runners (transparent)

## Goal

Users and models should **not** invent `~/.claude/plugins/cache/.../1.2.x` paths or
debug env vars. The host points at the skill; the skill finds the plugin.

## How OpenAI codex-for-Claude does it

Primary surface is **`commands/`** with harness expansion of
`${CLAUDE_PLUGIN_ROOT}` (bang lines / command frontmatter). That is transparent for
slash when the harness injects env. They largely avoid Skill-tool → bare Bash.

## How grok-skills does it (Skill-tool first)

### Transparent path (preferred)

1. Skill tool loads a skill and shows **Base directory for this skill**  
   (absolute path to `…/skills/<name>/`).
2. Model runs only:

```bash
SKILL_BASE='<that absolute path>'
node "$SKILL_BASE/run.mjs" <mode> [args...]
```

3. `skills/<name>/run.mjs` is **self-locating** (`import.meta.url` → plugin root →
   `scripts/grok-companion.mjs`). No `CLAUDE_PLUGIN_ROOT` required in the shell.

Shared logic: `plugin/scripts/lib/skill-run.mjs`.

### Optional env path

If the host already set `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` (hooks, some agents):

```bash
node "${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}/scripts/grok-companion.mjs" <mode> ...
```

Prefer `$SKILL_BASE/run.mjs` whenever the Skill tool loaded the skill.

### Codex custom agents

Managed agents under `~/.codex/agents/` already embed an absolute
`GROK_COMPANION=…/grok-companion.mjs` at SessionStart. No skill base needed.

### Never invent

Do **not** construct versioned cache paths by guessing a plugin version.
Do **use** Skill base + `run.mjs`, host env, or managed agent companion paths.

### Uninstall managed Codex agents

```bash
node "$SKILL_BASE/run.mjs" setup --remove-codex-agents
```

(Disable/uninstall the plugin first if you do not want SessionStart to reinstall them.)

## Resolver library (advanced / tests)

- `plugin/scripts/lib/resolve-plugin-root.mjs` — pure resolve helpers
- `plugin/scripts/resolve-plugin-root.mjs` — CLI
- `plugin/scripts/lib/skill-run.mjs` — spawn companion from skill entry
