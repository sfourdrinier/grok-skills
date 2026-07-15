<!-- docs/research/2026-07-14-codex-port-research.md -->

# Codex port research (Grok reason --web, runId 20260715T062727Z-35401d)

UNVERIFIED LLM + web research produced by the grok wrapper itself. The exact
Codex manifest/format specifics below MUST be verified against official Codex
docs before building the Phase 3 Codex adapter. Note: this run returned NO
structured citations (the wrapper does not capture them yet - that is Wave 1),
so treat inline source claims as leads to confirm.

---

I'll research official OpenAI Codex docs on plugins, custom commands, subagents, MCP, and packaging so we can map your Claude Code plugin to Codex cleanly.Fetching official Codex docs on plugins, skills, slash commands, subagents, and MCP.Fetching docs on custom prompts/slash commands, hooks, and agent-friendly CLIs.## Short answer

**Yes — this can become a Codex plugin.** Codex (as of mid-2026) has a first-class plugin system that can ship **skills**, **MCP servers**, **hooks**, and install-surface metadata. That maps cleanly onto a Claude Code plugin that is mostly slash commands + a subagent + optional hook over a hardened Python CLI.

The recommended shape is not “port Claude’s plugin format 1:1,” but:

1. Keep `grok_agent.py` as the durable interface (one JSON envelope on stdout).
2. Teach Codex how to call it via a **Skill** (and optionally wrap it as an **MCP tool**).
3. Package both in a Codex **plugin** for install/distribution.
4. Use a **custom agent TOML** only if you need a specialized worker role, not as the primary packaging unit.

---

## 1. What Codex supports (exact mechanisms)

| Capability | Supported? | Mechanism / format | Official surface |
|---|---|---|---|
| User-installable **plugins** | Yes | `.codex-plugin/plugin.json` + marketplace | `/plugins`, `codex plugin …` |
| **Skills** (reusable workflows) | Yes | `skills/<name>/SKILL.md` (+ optional scripts/refs) | `$skill-name`, `/skills` |
| **Custom slash commands** | Partial / deprecated | `~/.codex/prompts/*.md` → `/prompts:<name>` | Prefer skills |
| **Custom subagents** | Yes | `~/.codex/agents/*.toml` or `.codex/agents/*.toml` | Spawn / `/agent` |
| **MCP servers** | Yes | `~/.codex/config.toml` or plugin `.mcp.json` | `codex mcp add`, `/mcp` |
| **AGENTS.md** | Yes | Global + project instruction chain | Auto-loaded |
| **Hooks** | Yes | `hooks/hooks.json` or plugin-bundled hooks | `/hooks` trust review |
| **config.toml** | Yes | `~/.codex/config.toml`, project `.codex/config.toml` | Shared by CLI / IDE / app |

### Plugins

Official plugins can bundle skills, MCP-backed apps, MCP servers, hooks, browser extensions, and scheduled-task templates. Install via plugin directory (`/plugins` in CLI).

Manifest lives at `.codex-plugin/plugin.json`. Minimal example from official docs:

```json
{
  "name": "my-first-plugin",
  "version": "1.0.0",
  "description": "Reusable greeting workflow",
  "skills": "./skills/"
}
```

Full plugin layout:

```text
my-plugin/
  .codex-plugin/plugin.json   # required
  skills/                     # optional
  hooks/hooks.json            # optional
  .mcp.json                   # optional MCP servers
  .app.json                   # optional app/connector mappings
  assets/                     # optional
```

Marketplaces:

- Repo: `$REPO_ROOT/.agents/plugins/marketplace.json`
- Personal: `~/.agents/plugins/marketplace.json`
- Legacy-compatible: `$REPO_ROOT/.claude-plugin/marketplace.json` (Codex can read it)
- Sources: local path, git, npm package

CLI marketplace management:

```bash
codex plugin marketplace add owner/repo
codex plugin marketplace add ./local-marketplace-root
codex plugin marketplace list
```

### Skills (primary reusable-command surface)

A skill is a directory with `SKILL.md` (`name` + `description` required). Codex uses progressive disclosure: metadata first, full instructions only when selected. Explicit invoke with `$skill-name` (or slash-list entry); implicit invoke by description match.

```markdown
---
name: skill-name
description: Explain exactly when this skill should and should not trigger.
---

Skill instructions for Codex to follow.
```

Skill locations:

- Repo: `.agents/skills` (cwd up to repo root)
- User: `~/.agents/skills`
- Admin: `/etc/codex/skills`
- System: bundled
- Distributed: via plugins under `skills/`

### Custom prompts / slash commands (deprecated)

Official docs mark **custom prompts as deprecated** in favor of skills. They still work as local Markdown under `~/.codex/prompts/` and appear as `/prompts:<name>`. They are **not repo-shared** and require explicit invocation.

Slash command UI also notes: enabled skills appear in the `/` list; custom prompts appear as `/prompts:<name>`.

### Subagents

Native subagent workflows are on by default. You can also define **custom agents** as standalone TOML:

```toml
# ~/.codex/agents/reviewer.toml  (or .codex/agents/reviewer.toml)
name = "reviewer"
description = "PR reviewer focused on correctness, security, and missing tests."
developer_instructions = """
Review code like an owner.
Prioritize correctness, security, behavior regressions, and missing test coverage.
"""
model = "gpt-5.4"
model_reasoning_effort = "high"
sandbox_mode = "read-only"
```

Required fields: `name`, `description`, `developer_instructions`. Optional: model, sandbox, MCP servers, skills config, nicknames.

Global knobs in config:

```toml
[agents]
max_threads = 6
max_depth = 1
```

### MCP

STDIO and streamable HTTP servers are supported. Shared config in `~/.codex/config.toml` (or project `.codex/config.toml` when trusted):

```toml
[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
```

```bash
codex mcp add context7 -- npx -y @upstash/context7-mcp
```

Plugin-bundled MCP uses `.mcp.json` and is policy-controlled under `plugins.<plugin>.mcp_servers.<server>`.

### AGENTS.md

Instruction layering only (not executable tools): global `~/.codex/AGENTS.md`, then project walk from root to CWD. Good for always-on policy (“prefer the grok agent CLI for X”), not for packaging a product.

### Hooks

Full lifecycle hooks (`SessionStart`, `PreToolUse`, `Stop`, etc.). Plugins can ship `hooks/hooks.json`. Plugin hooks get `PLUGIN_ROOT` / `PLUGIN_DATA` and also `CLAUDE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_DATA` for Claude-plugin compatibility. **Plugin hooks are not auto-trusted** — user must review/trust via `/hooks`.

---

## 2. Closest Codex equivalents to Claude Code’s surface

| Claude Code concept | Closest Codex equivalent | Notes |
|---|---|---|
| Plugin package | **Codex plugin** (`.codex-plugin/plugin.json` + marketplace) | First-class now; not a missing feature |
| Slash command | **Skill** (`$name` / slash list entry) | Custom prompts `/prompts:name` exist but are **deprecated** |
| Subagent definition | **Custom agent TOML** + native spawn | Parallel workers with own context; not the same “plugin-declared subagent” file layout as Claude |
| Hook | **hooks/hooks.json** (user/project/plugin) | Trust review required for non-managed hooks |
| Thin wrapper over CLI | **Skill that shells out** (official pattern) and/or **MCP tool wrapper** | Official “Create a CLI Codex can use” is CLI + companion skill |
| Always-on repo policy | **AGENTS.md** | Instructions only |

Important UX difference: Claude-style “type `/foo` and a specialized agent runs” becomes Codex “type `$foo` (skill) and/or ask the main agent to spawn a named custom agent.” Skills are the primary reusable workflow unit; custom agents are role/config profiles for delegated work.

---

## 3. Best way to expose your Python CLI to Codex

### Recommendation (best fit): **Plugin = Skill (+ optional MCP) over the existing CLI**

Why this wins for your architecture:

- Your hard work is already in `grok_agent.py` with a **single JSON envelope on stdout**. Codex’s own guidance is to give agents a **composable CLI + companion skill**, not to rewrite the CLI into agent-native code.
- A **skill** is the direct replacement for Claude slash-command prompts: when/how to run the CLI, parse the JSON, and what not to do.
- An **MCP server** is the better fit *if* you want first-class tool schemas, approval modes, and structured results without relying on shell + stdout parsing. Your JSON envelope maps naturally to tool I/O.
- **AGENTS.md alone** is too weak (no install unit, no progressive disclosure, no packaging).
- **Custom prompts** are deprecated and user-local only — poor distribution story.
- **Custom agent TOML alone** does not package skills/MCP/hooks for install; use it only if the worker needs different model/sandbox/instructions.

### Decision tree

| Goal | Package as |
|---|---|
| Same workflow as Claude slash command for humans + agent | **Skill** in a plugin |
| Typed tools / structured calls / approval policy | **MCP server** (plugin-bundled or `codex mcp add`) wrapping the CLI |
| Specialized parallel worker with own instructions/model | **Custom agent TOML** that is told to use the skill/MCP/CLI |
| Always prefer this tool in a repo | **AGENTS.md** one-liner + installed skill/plugin |
| Lifecycle injection (session start context, stop checks) | **Plugin hooks** (with trust friction) |

### Practical hybrid (closest to your Claude plugin)

```text
grok-agent-codex-plugin/
  .codex-plugin/plugin.json
  skills/
    grok-agent/SKILL.md          # how/when to run CLI; parse JSON envelope
    grok-review/SKILL.md         # if you had multiple slash commands
  .mcp.json                      # optional: expose run_agent tool
  hooks/hooks.json               # optional: only if you truly need lifecycle
```

Keep `grok_agent.py` **outside** the agent loop as the source of truth. Skill/MCP are thin adapters.

If you only need one path: start with **Skill + PATH-installed CLI**. Add MCP if tool-call reliability or multi-client reuse matters more than simplicity.

---

## 4. Concrete packaging instructions

### Step A — Make the CLI agent-friendly (keep your contract)

Your existing contract is already what Codex wants:

- One command on PATH (e.g. `grok-agent`)
- Deterministic flags
- **Exactly one JSON object on stdout** for the final result
- Progress on stderr or a separate JSONL stream (don’t pollute the result envelope)
- Clear non-zero exit codes on failure
- `--help` that documents subcommands

Install so `command -v grok-agent` works from any folder (pipx/venv entrypoint, or plugin `scripts/` with absolute path via `$PLUGIN_ROOT` if you ship the binary with the plugin).

### Step B — Author the companion skill

`skills/grok-agent/SKILL.md`:

```markdown
---
name: grok-agent
description: >
  Run the hardened Grok coding agent CLI for multi-step coding tasks that need
  a single structured JSON result. Use when the user asks to run grok-agent,
  or needs a delegated coding agent with a JSON envelope. Do not use for simple
  one-line shell edits.
---

# Grok Agent

## When to use
- User invokes $grok-agent or asks for the Grok coding agent workflow.

## How to run
1. Ensure `grok-agent` is on PATH (or use `${PLUGIN_ROOT}/bin/grok-agent` if bundled).
2. Run with the user's task as the prompt argument.
3. Capture **stdout only** as the result envelope (single JSON object).
4. Do not invent fields; parse the envelope and summarize status/errors from it.
5. On non-zero exit, surface stderr and the JSON error fields if present.

## Example
```bash
grok-agent --json "Implement X and report status"
```

## Output contract
- stdout: one JSON object (result envelope)
- progress: JSONL on stderr (optional; do not mix into stdout)
```

If you need progressive disclosure of schemas, put the JSON schema under `skills/grok-agent/references/result-envelope.md` and tell the skill to read it when validating output.

### Step C — Optional MCP wrapper (best structured fit)

` .mcp.json`:

```json
{
  "grok_agent": {
    "command": "python3",
    "args": ["${PLUGIN_ROOT}/mcp_server.py"]
  }
}
```

`mcp_server.py` (conceptually):

- Expose tools like `run_task(prompt: str, …) -> object`
- Internally: `subprocess.run(["grok-agent", …], capture_output=True)`
- Parse the single JSON envelope from stdout
- Return that object as the MCP tool result
- Map exit codes to MCP errors

Then declare in plugin manifest:

```json
{
  "name": "grok-agent",
  "version": "0.1.0",
  "description": "Hardened Grok coding agent for Codex",
  "skills": "./skills/",
  "mcpServers": "./.mcp.json",
  "interface": {
    "displayName": "Grok Agent",
    "shortDescription": "Run the Grok agent CLI with a structured JSON result",
    "category": "Productivity",
    "defaultPrompt": [
      "Use Grok Agent to implement the requested change and return the JSON status."
    ]
  }
}
```

### Step D — Optional custom agent (only if you need a worker role)

`.codex/agents/grok-worker.toml` (project) or ship install docs for `~/.codex/agents/`:

```toml
name = "grok_worker"
description = "Delegates multi-step coding to the grok-agent CLI/MCP and returns a structured summary."
developer_instructions = """
You are a thin orchestrator for the Grok agent.
Always invoke the grok-agent skill or MCP tool.
Parse the JSON envelope; do not re-run freeform coding yourself unless the tool fails.
Return a concise status, changed files, and next steps from the envelope.
"""
```

Note: official plugin packaging docs emphasize skills/MCP/hooks/apps — **custom agent TOML is not listed as a first-class plugin component**, so treat agent files as optional project/user config layered beside the plugin.

### Step E — Optional hook (parity with Claude hook)

Only if you truly need lifecycle behavior (inject context at `SessionStart`, validation on `Stop`). Plugin:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${PLUGIN_ROOT}/hooks/session_start.py",
            "statusMessage": "Loading Grok agent context"
          }
        ]
      }
    ]
  }
}
```

Users must **trust** the hook in `/hooks` before it runs. That is a Codex-side friction Claude users may not expect.

### Step F — Marketplace + install

Repo marketplace (`.agents/plugins/marketplace.json`):

```json
{
  "name": "local-repo",
  "interface": { "displayName": "Local Plugins" },
  "plugins": [
    {
      "name": "grok-agent",
      "source": { "source": "local", "path": "./plugins/grok-agent" },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
```

User flow:

```bash
codex
/plugins          # install from marketplace
# start a NEW session
$grok-agent implement the feature and return status
```

Or for MCP-only users without a plugin:

```bash
codex mcp add grok-agent -- python3 /path/to/mcp_server.py
```

---

## What Codex does **not** support (or supports differently) vs your Claude plugin

| Claude Code dependency | Codex reality |
|---|---|
| Claude plugin layout / `.claude-plugin` as primary package | Codex primary is **`.codex-plugin/plugin.json`**. Codex *can* read a legacy `.claude-plugin/marketplace.json`, but author new packages in Codex format. |
| Slash commands as first-class plugin commands | Replaced by **skills** (`$name`). `/prompts:*` custom prompts are **deprecated**, user-local, not for distribution. |
| Plugin-declared subagent as a first-class plugin component | Subagents exist, but custom agents are **TOML config layers** under `.codex/agents/`, not a standard plugin subfolder in the official plugin structure. |
| Hooks auto-enabled with plugin install | Plugin hooks load, but **require explicit trust review** (`/hooks`). Not silent enable. |
| Implicit “command tool” that always runs your CLI | No special SlashCommand tool for custom commands; agent runs shell or MCP tools. Reliability comes from skill instructions + good CLI contract. |
| Progress JSONL as a first-class agent stream | Codex doesn’t have a special protocol for your JSONL progress. Use stderr for humans, MCP progress if you build it, or ignore intermediate lines and only parse final stdout JSON. |
| One shared Claude/Codex binary package without adapters | Possible core CLI, but you still need **Codex skill/MCP/plugin wrappers** (and Claude wrappers separately). Hooks have Claude-compat env vars, which helps a bit. |

---

## Bottom line

| Question | Answer |
|---|---|
| Can this become a Codex plugin? | **Yes.** |
| Exact mechanism? | Official **plugins** (manifest + marketplace), **skills**, **MCP**, **hooks**, **AGENTS.md**, **custom agent TOML**, **config.toml**. |
| Closest to slash commands? | **Skills** (`$name`); not deprecated custom prompts. |
| Closest to subagents? | Native spawn + **custom agent TOML**. |
| Best fit for your Python CLI + JSON envelope? | **Plugin with companion skill** (official CLI+skill pattern); add **MCP wrapper** if you want structured tools; keep CLI as the hard boundary. |
| Biggest gaps vs Claude plugin? | Skills replace slash commands; custom prompts deprecated; custom agents aren’t packaged the same way; hooks need trust; no first-class JSONL progress protocol. |

### Suggested migration path

1. **Day 1:** skill that shells out to `grok-agent` + install on PATH (prove parity).  
2. **Day 2:** wrap the same CLI as an MCP tool for structured calls.  
3. **Day 3:** package skill + MCP as a Codex plugin with marketplace entry.  
4. **Optional:** custom agent TOML for delegated “worker” usage; hooks only if lifecycle is essential.

Official docs to keep open while implementing:

- [Plugins](https://developers.openai.com/codex/plugins) / [Build plugins](https://developers.openai.com/codex/build-plugins)
- [Build skills](https://developers.openai.com/codex/build-skills)
- [MCP](https://developers.openai.com/codex/mcp)
- [Subagents](https://developers.openai.com/codex/subagents)
- [Hooks](https://developers.openai.com/codex/hooks)
- [AGENTS.md](https://developers.openai.com/codex/agent-configuration/agents-md)
- [Custom prompts (deprecated)](https://learn.chatgpt.com/docs/custom-prompts)
- [Create a CLI Codex can use](https://developers.openai.com/codex/use-cases/agent-friendly-clis)
