# Plugin root and skill runners (transparent)

## Goal

Users and models should **not** invent `~/.claude/plugins/cache/.../1.2.x` paths or
debug env vars. The host points at the skill; the skill finds the plugin.

## How OpenAI codex-for-Claude does it

Primary surface is **`commands/`** with harness expansion of
`${CLAUDE_PLUGIN_ROOT}` (bang lines / command frontmatter). That is transparent for
slash when the harness injects env. They largely avoid Skill-tool → bare Bash.

## How grok-skills does it (Skill-tool first)

### Entrypoints (priority)

1. **Claude Code `bin/` shim (entrypoint #1 on Claude Code):** while the plugin is
   enabled, Claude Code auto-discovers `plugin/bin/*` and puts bare commands on the
   Bash tool PATH. Prefer:

```bash
grok-skills <mode> [args...]
```

   The shim (`plugin/bin/grok-skills`) self-locates `scripts/grok-companion.mjs` from
   its own path and forwards argv with exit-code passthrough. No manifest field is
   required for `bin/` discovery.

2. **`$SKILL_BASE/run.mjs` everywhere else** (Skill tool, Codex, any host without
   plugin `bin/` support). Codex has **no** plugin `bin/` support - skills and
   Codex agents must keep using self-locating `run.mjs` / `agents/run.mjs`.

### Transparent path (preferred for skills)

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

**Stale env after upgrade:** `run.mjs` and `agents/run.mjs` always force
`CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` on the child to the **entry-derived** install
tree (not whatever the parent shell still has). `grok-companion.mjs` and the
SessionStart hook do the same from their own path. That prevents a leftover env
from an old cache version from loading a mixed old/new install.

### Optional env path

If the host already set `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` (hooks, some agents):

```bash
node "${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}/scripts/grok-companion.mjs" <mode> ...
```

Prefer `$SKILL_BASE/run.mjs` whenever the Skill tool loaded the skill (it wins
over a stale env). On Claude Code with the plugin enabled, prefer bare
`grok-skills` when `command -v grok-skills` succeeds.

### Claude plugin agents (aligned)

Shim-first (Claude Code PATH), then self-locating `agents/run.mjs` (plugin root =
parent of `agents/`):

```bash
if command -v grok-skills >/dev/null 2>&1; then
  GROK_RUN() { grok-skills "$@"; }
else
  PLUGIN_INSTALL="${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}"
  GROK_RUN() { node "$PLUGIN_INSTALL/agents/run.mjs" "$@"; }
fi
GROK_RUN code ...
```

`agents/run.mjs` locates the companion from its own path (same family as skills).

After a marketplace upgrade, reopen the session so the host reinjects the new
plugin root into agent shells (and reloads `bin/` on PATH). Skills avoid that via
Skill-tool base paths; Claude agents still need a fresh host env to *find*
`agents/run.mjs` when the shim is not on PATH (once running, entry-forcing keeps
the tree consistent).

### Codex custom agents

SessionStart installs managed TOML with absolute **`GROK_AGENT_RUN=…/agents/run.mjs`**
(not a guessed cache path). Agents run:

```bash
node "$GROK_AGENT_RUN" code ...
```

Codex has no plugin `bin/` support - do not rely on a bare `grok-skills` command.

### Never invent

Do **not** construct versioned cache paths by guessing a plugin version.
Do **use** Skill base + `run.mjs`, host env, bare `grok-skills` (Claude Code),
or managed `GROK_AGENT_RUN` / `agents/run.mjs` paths.

### Uninstall managed Codex agents

```bash
node "$SKILL_BASE/run.mjs" setup --remove-codex-agents
```

(Disable/uninstall the plugin first if you do not want SessionStart to reinstall them.)

## Resolver library (advanced / tests)

- `plugin/scripts/lib/resolve-plugin-root.mjs` - pure resolve helpers
- `plugin/scripts/resolve-plugin-root.mjs` - CLI
- `plugin/scripts/lib/skill-run.mjs` - spawn companion from skill entry
