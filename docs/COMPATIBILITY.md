<!-- docs/COMPATIBILITY.md -->

# Compatibility (Claude Code + Codex / ChatGPT)

## Wrapper lifecycle (1.3.0+)

Status is strictly read-only. Failed/interrupted targets return exit 1 with a
well-formed status envelope (relay the JSON regardless). Durable runs seed
`run.json` before publishing a run id; terminal results use envelope-first
persist via a spawn finalize worker.

## Opt-in isolated review (1.4.0+)

`review --isolated` (hardened only) creates an owned worktree under the state
root, applies tracked dirty against a pinned base SHA, and cleans up after the
run. `--base` alone is framing only (live checkout). Direct mode rejects
`--isolated` (`isolation-unavailable`). Fail closed; no silent live fallback.

## Implementation handoff (1.6.0+)

`code` may take optional `--contract-file` (operator-trusted writeScopes +
requiredValidation). After Grok, the wrapper writes
`implementation-handoff.json` + `artifacts/implementation.patch` under the run
dir for isolated integration paths. Parents must call **`handoff --run-id`**
before integrating **code-mode** auto/review results; dual-condition ready
requires ready manifest **and** a success terminal envelope **and** patch
rehash. Notifications are not ready. Integrate is **mode-aware** (direct lands
live; auto may apply; review is parent apply) - see
`plugin/references/integration-modes.md`. Handoff skill itself never applies.
Details: `plugin/references/implementation-handoff.md`.

## Implementation contract load-time caps (2.0.0+)

Operator contracts are validated **before** Grok spawns
(`implementation-contract-invalid` fail-closed). Load-time invariants:

| Cap / rule | Value |
|------------|-------|
| `schemaVersion` | must be **1** (only version accepted) |
| `objective` | max **2000** characters when present |
| `acceptanceCriteria` | max **32** items; each item max **500** chars after strip |
| `writeScopes` | non-empty array of `{kind: file\|subtree, path}` |
| `requiredValidation` | optional array; when present each entry needs non-empty string `argv[]` (no embedded NUL) |
| Path normalization | operator paths reject Windows drive forms; Git-reported paths keep colons/backslashes as filename characters |

Constants live in `plugin/wrapper/scripts/groklib/implementation_contract.py`
(`OBJECTIVE_MAX_CHARS`, `ACCEPTANCE_CRITERIA_MAX_ITEMS`,
`ACCEPTANCE_CRITERION_MAX_CHARS`) and are mirrored on handoff
`contractSummary` so a tampered manifest cannot push multi-MB display fields.

## Migration compatibility (2.0.0 peer-native)

| Surface | Behavior |
|---------|----------|
| **integration default** | Product (companion/skills) defaults to **direct** after per-repo setup consent; bare `python3 …/grok_agent.py code` without `--integration` still defaults to **worktree** (fail-closed isolation for un-consented bare calls). |
| **ACP peer channel** | Default on for `grok-engineer-coder`. Opt out with `GROK_DISABLE_ACP=1` (one-shot `code` fallback). `GROK_EXPERIMENTAL_ACP` is no longer a hard enable gate. |
| **runMode vs integration** | Orthogonal axes that both use the word "direct". runMode=direct = installed CLI home; integration=direct = live-tree edits. See integration-modes.md. |
| **handoff vs peer** | `/grok:handoff` remains **code-mode only** and refuses peer runIds (`handoff-unavailable`). Peer integrate runs at `peer stop` per active integration mode. |
| **Older contracts** | schemaVersion must be 1; missing optional display fields normalize to empty; oversized objective/criteria fail at load (no silent truncation). |

## Completion notifications (1.5.0+)

Companion-only push after a terminal **live** run (review/reason/code/verify/
adversarial-review). Not status/jobs/result/setup alone.

| Pref | Behavior |
|------|----------|
| `notificationMode: off` (default) | No push |
| `auto` | Native OS notify only when `GROK_COMPANION_EXECUTION_CONTEXT=background` |
| `native` | OS notify (macOS/Linux) for FG and BG |
| `webhook` | POST JSON if `notificationWebhookUrl` set |

At-most-once **attempt** via `runs/<runId>/notified.json` for hardened durable
runs (no auto-retry; not exactly-once). Skills/agents must set execution context
per `plugin/references/execution-context.md`. Context is never forwarded to the
Python wrapper.

**1.5.0 residuals deferred to PR5 (1.7.0):**

| Item | Note |
|------|------|
| Operator re-attempt | Explicit re-fire after failed/stuck notify (may duplicate) |
| Direct-mode push notify | Job-scoped marker home (direct has no wrapper `runs/<id>`) |
| Headless / native honesty | Setup + docs: native needs a desktop session (macOS/Linux); **Windows** stays unsupported for native toast - use **webhook** |

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
  `~/.claude/plugins/cache` - paths like `../shared` do **not** survive install.
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
  we materialize `plugin/codex-agents/*.toml` into `~/.codex/agents/` (or project
  `.codex/agents/` when workspace prefs scope is `project`) on SessionStart
  with absolute `GROK_AGENT_RUN` → `agents/run.mjs` (v1.2.5+; SessionStart since
  v1.2.1). Interface category: **Development & Workflow**. See **Upstream gaps**.
- Install sources (both hosts):

  | Source | Claude | Codex |
  |--------|--------|-------|
  | GitHub shorthand | `sfourdrinier/grok-skills` | `sfourdrinier/grok-skills` |
  | Git URL | `https://github.com/sfourdrinier/grok-skills.git` | same / SSH |
  | Local path (dev) | absolute path to repo root | same |

  Marketplace JSON still uses relative `./plugin` - after a git marketplace add,
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

## Upstream gaps

Re-check these at each release (links may close or change behavior). Workarounds
in this repo must stay honest about what the host still cannot do.

| Upstream | Gap | Our workaround / posture |
|----------|-----|---------------------------|
| [openai/codex#18988](https://github.com/openai/codex/issues/18988) | Plugins cannot bundle custom agents the way Claude loads `plugin/agents/` | SessionStart + optional `setup` materialize `plugin/codex-agents/*.toml` into `~/.codex/agents/` (or project `.codex/agents/` when `setup --codex-agents-scope project`) with absolute `GROK_AGENT_RUN` |
| [openai/codex#18308](https://github.com/openai/codex/issues/18308) | Plugin hooks are not auto-trusted on install | Stop-review gate and SubagentStop handoff nudge stay **dormant until trusted** via `/hooks` (honest default). Skills and agent materialization do not depend on hook trust. |

## Not required for core use

- MCP / `.app.json` connectors (this plugin is CLI-wrapper based, not an OAuth app)
- Official Anthropic / OpenAI public directory listing (self-hosted marketplace works)
