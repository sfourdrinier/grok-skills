<!-- docs/COMPATIBILITY.md -->

# Compatibility (Claude Code + Codex / ChatGPT)

Verified against local installs on 2026-07-15:

| Host | Version tested |
|------|----------------|
| Claude Code CLI | **2.1.210** |
| Codex CLI | **0.144.4** |

Official docs consulted:

- Claude Code plugins / marketplaces: [code.claude.com/docs/en/plugins](https://code.claude.com/docs/en/plugins), [plugins-reference](https://code.claude.com/docs/en/plugins-reference), [plugin-marketplaces](https://code.claude.com/docs/en/plugin-marketplaces), [discover-plugins](https://code.claude.com/docs/en/discover-plugins)
- Codex / ChatGPT plugins: [learn.chatgpt.com/docs/build-plugins](https://learn.chatgpt.com/docs/build-plugins) (Codex “Build plugins”), [learn.chatgpt.com/codex/hooks](https://learn.chatgpt.com/codex/hooks)
- Reference shapes: OpenAI `plugins` repo (e.g. Figma `.codex-plugin/plugin.json`), community marketplaces using `.agents/plugins/marketplace.json`

## Claude Code (2.1.x)

What we match:

- Marketplace at `.claude-plugin/marketplace.json` with relative plugin `source: "./plugin"`
- Plugin root contains `.claude-plugin/plugin.json` plus `skills/`, `agents/`, `hooks/`, `scripts/`
- Skills live under `skills/<name>/SKILL.md` (preferred over flat `commands/`)
- Namespaced skills: `/grok:review`, `/grok:preflight`, …
- **Critical:** plugin install copies only the plugin directory into
  `~/.claude/plugins/cache` — paths like `../shared` do **not** survive install.
  The Python wrapper is therefore **bundled** at `plugin/wrapper/` so the cache
  still contains `wrapper/scripts/grok_agent.py`.
- `claude plugin validate ./plugin --strict` and `claude plugin validate .` pass

## Codex CLI + ChatGPT desktop (Codex surface)

What we match:

- Repo marketplace: `.agents/plugins/marketplace.json` with
  `source: { "source": "local", "path": "./plugin" }`, `policy`, `category`,
  `displayName`, `icon`
- Plugin dual-manifest: `plugin/.codex-plugin/plugin.json` with `skills`,
  `hooks`, Figma-style `interface` (displayName, logos, defaultPrompt, category).
  Codex does not yet register plugin-bundled custom agents (openai/codex#18988);
  we materialize `plugin/codex-agents/*.toml` into `~/.codex/agents/` on SessionStart
  with an absolute companion path (v1.2.1+).
  **Development & Workflow**)
- Install sources (both hosts):

  | Source | Claude | Codex |
  |--------|--------|-------|
  | GitHub shorthand | `sfourdrinier/grok-skills` | `sfourdrinier/grok-skills` |
  | Git URL | `https://github.com/sfourdrinier/grok-skills.git` | same / SSH |
  | Local path (dev) | absolute path to repo root | same |

  Marketplace JSON still uses relative `./plugin` — after a git marketplace add,
  the host clones the repo and resolves that path inside the clone. No local
  path is required for end users.

- Install verified (local path and git-style marketplace layout):

  ```bash
  # Preferred once the repo is reachable for the installing user:
  codex plugin marketplace add sfourdrinier/grok-skills
  codex plugin add grok@grok-skills

  # Dev / private checkout:
  codex plugin marketplace add /path/to/grok-skills
  codex plugin add grok@grok-skills
  ```

  Result: `grok@grok-skills` **installed, enabled**; cache at
  `~/.codex/plugins/cache/grok-skills/grok/<version>/` includes
  `wrapper/`, `skills/`, `scripts/`, assets.
- Custom agents: Claude loads `plugin/agents/` (`grok-engineer-coder`,
  `grok-rescue`). Codex: run companion `setup` to install
  `plugin/codex-agents/*.toml` into `~/.codex/agents/`.
- Plugin env: Codex sets `PLUGIN_ROOT` and also `CLAUDE_PLUGIN_ROOT` for
  compatibility. Companion resolves both. Preflight succeeded against the
  **Codex cache** with only `PLUGIN_ROOT` set.
- Hooks: shared `Stop` event exists on both Claude Code and Codex. Gate emits
  JSON always (`{"continue":true}` allow / `{"decision":"block","reason"}` block)
  so Codex’s “JSON required on Stop exit 0” rule is satisfied. Plugin hooks still
  require user trust review in Codex (`/hooks`) before they run.

## ChatGPT desktop app

Codex in the ChatGPT desktop app reads the same marketplaces:

- Repo: `$REPO_ROOT/.agents/plugins/marketplace.json`
- Legacy-compatible: `$REPO_ROOT/.claude-plugin/marketplace.json`
- Personal: `~/.agents/plugins/marketplace.json`

After adding this repo (open as project or add marketplace), install **Grok Skills**
from the plugin directory UI and restart if prompted.

## Skill instructions

Each skill includes a **Harness compatibility** section so Claude Code and Codex
agents both know to:

1. Set `GROK_PLUGIN_ROOT` from `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT`
2. Invoke `node "$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs" …`
3. Relay the single JSON envelope on stdout verbatim
4. Never shell-evaluate free-text `--task "…"`

Claude-only UI (`AskUserQuestion`) is optional; Codex falls back to asking in chat.

## Not required for core use

- MCP / `.app.json` connectors (this plugin is CLI-wrapper based, not an OAuth app)
- Official Anthropic / OpenAI public directory listing (self-hosted marketplace works)
