<!-- docs/COMPATIBILITY.md -->

# Compatibility (Claude Code + Codex / ChatGPT)

## Wrapper lifecycle (1.3.0+)

Status is strictly read-only. Failed/interrupted targets return exit 1 with a
well-formed status envelope (relay the JSON regardless). Durable runs seed
`run.json` before publishing a run id; terminal results use envelope-first
persist via a spawn finalize worker.

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
  with absolute `GROK_AGENT_RUN` → `agents/run.mjs` (v1.2.5+; SessionStart since
  v1.2.1). Interface category: **Development & Workflow**.
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
  `grok-rescue`) via self-locating `agents/run.mjs`. Codex: **SessionStart**
  materializes `plugin/codex-agents/*.toml` into `~/.codex/agents/` with absolute
  `GROK_AGENT_RUN` (optional `setup --force-codex-agents` to overwrite user edits).
- Plugin env: Codex sets `PLUGIN_ROOT` and also `CLAUDE_PLUGIN_ROOT` for
  compatibility. Entry runners and the companion force the install tree they live
  in so stale env after upgrade cannot mix versions. Preflight succeeded against
  the **Codex cache** with only `PLUGIN_ROOT` set.
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

Each skill includes a **How to run (transparent)** section so Claude Code and Codex
agents both know to:

1. Prefer the Skill tool’s base directory and `node "$SKILL_BASE/run.mjs" …`
   (self-locating; no env required). Host-set `CLAUDE_PLUGIN_ROOT` /
   `PLUGIN_ROOT` is optional; entry runners force the install they live in so a
   stale env after upgrade cannot mix trees.
2. Alternatively: `node "${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}/scripts/grok-companion.mjs" …`
3. Relay the single JSON envelope on stdout verbatim
4. Never shell-evaluate free-text `--task "…"`

Claude-only UI (`AskUserQuestion`) is optional; Codex falls back to asking in chat.

## Incomplete / cancelled runs (envelope)

Live modes default to **no** `--max-turns` (unlimited until EndTurn / timeout).
If the operator sets `--max-turns` and Grok stops at the budget (often as
`stopReason: Cancelled` with `numTurns` at the cap), or stops mid-run as
`Cancelled` with real text/structured findings, the wrapper returns
`status: success` with `response` populated and a **warning** that findings may
be incomplete. Empty shells are not salvaged (`findings: []` / `null`,
placeholder-only findings, blank text).

## Not required for core use

- MCP / `.app.json` connectors (this plugin is CLI-wrapper based, not an OAuth app)
- Official Anthropic / OpenAI public directory listing (self-hosted marketplace works)
