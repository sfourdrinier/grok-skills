<!-- plugin/references/README.md -->

# grok plugin references

This plugin is a thin surface over the hardened Grok CLI wrapper. It adds no
safety logic of its own. Skills and agents shell to the companion, which runs
the wrapper and relays the single JSON result envelope on stdout VERBATIM.

**Agents:** `grok-engineer-coder` (implement in isolated worktree; host
orchestrates) and `grok-rescue` (diagnosis / second opinion). Claude loads
`plugin/agents/` automatically. Codex agents auto-install on **SessionStart**
into `~/.codex/agents/` (absolute `agents/run.mjs`); optional **setup** can force
or remove managed agents.

**Invocation:** Claude uses `/grok:…` skills; Codex uses the skill picker /
`$name` for the same skill names. Prefer each skill’s `$SKILL_BASE/run.mjs`.

## Activating in Claude Code

Prerequisites: `grok` CLI installed and authenticated, `node` and `python3` on
PATH, macOS for live modes.

Preferred — install the marketplace from GitHub (no manual clone):

```
/plugin marketplace add sfourdrinier/grok-skills
/plugin install grok@grok-skills
```

Also accepted: full git URL, `owner/repo@ref`, or a local checkout path for
development. Reload plugins, confirm `/grok:` autocomplete, then
`/grok:preflight`.

Local development without a marketplace:

```
claude --plugin-dir /absolute/path/to/grok-skills/plugin
```

The wrapper is bundled at `${CLAUDE_PLUGIN_ROOT}/wrapper/scripts/grok_agent.py`.
No `GROK_AGENT_WRAPPER` is required for a standard install.

## Activating in Codex

Repo marketplace: `.agents/plugins/marketplace.json` (relative source
`./plugin` — resolved after Claude/Codex clone the marketplace root).

Preferred:

```
codex plugin marketplace add sfourdrinier/grok-skills
codex plugin add grok@grok-skills
```

Also accepted: HTTPS/SSH git URL, `owner/repo --ref <ref>`, or a local path.
Desktop app: add the same git marketplace (or open a clone once), then install
**grok**. Codex exports `PLUGIN_ROOT` (and usually `CLAUDE_PLUGIN_ROOT`). Prefer
Skill base + `run.mjs` over inventing cache paths.

After install, start a **new session** so SessionStart materializes
`~/.codex/agents/grok-*.toml` (`grok-engineer-coder`, `grok-rescue`). Optional
`setup --force-codex-agents` if you need to overwrite user-edited agents.

## What owns safety

The wrapper (`wrapper/scripts/grok_agent.py` + `wrapper/scripts/groklib/**`) owns
private auth-home isolation, worktree confinement, sandbox verification, rule
loading, secret scanning, and the fail-closed error model. See
`wrapper/SKILL.md`.

## Security model

Trusted-input developer tool. Enforced: write confinement, private auth home,
redacted single envelope, worktree isolation, gate-script integrity. Not a
sandbox against an adversarial model. Full notes:
[`../../docs/OPEN-SECURITY-DECISIONS.md`](../../docs/OPEN-SECURITY-DECISIONS.md).

## Wrapper resolution

`scripts/grok-companion.mjs` resolves `grok_agent.py` in this order:

1. `GROK_AGENT_WRAPPER` only if `GROK_ALLOW_WRAPPER_OVERRIDE=1` (tests / advanced)
2. `${CLAUDE_PLUGIN_ROOT}/wrapper/scripts/grok_agent.py`
3. `${PLUGIN_ROOT}/wrapper/scripts/grok_agent.py`
4. Derived from the companion script location (plugin root)

If none exist, the companion fails closed with an actionable message.

## Skill surface

Canonical table: root [README.md](../../README.md) (skills + agents). Summary:

| Skill | Wrapper mode | Notes |
|-------|--------------|-------|
| `/grok:preflight` | `preflight` | Readiness (runnable CLI, auth, sandbox) |
| `/grok:setup` | companion setup | Optional gate/mode/notifications; Codex agents auto on SessionStart |
| `/grok:review` | `review` | Full-context read-only; live checkout by default; opt-in `--isolated` worktree; `--base` framing only; `--web` opt-in |
| `/grok:adversarial-review` | `adversarial-review` | Hostile; web on by default |
| `/grok:dual-lens` | companion | Adversarial then ordinary review |
| `/grok:reason` | `reason` | Cold second opinion; web off by default |
| `/grok:code` | `code` | Isolated worktree implementation (+ optional `--contract-file`) |
| `/grok:verify` | `verify` | Hermetic verify; never `--web` |
| `/grok:handoff` | `handoff` | Verified implementation by **runId** (1.6.0+; dual-condition ready) |
| `/grok:debate` | companion | Two reason passes + synthesis |
| `/grok:status` / `jobs` / `result` / `cancel` | companion | Job inspection |
| `/grok:transfer` | companion | Claude session → task pack |
| `/grok:cleanup` | `cleanup` | Dry-run by default; `--confirm` removes |

## Implementation handoff (1.6.0+)

See [implementation-handoff.md](implementation-handoff.md). After `/grok:code`,
parents call `/grok:handoff --run-id` before any apply. Notify is not ready.
No auto-apply.

## Execution context and notifications (1.5.0+)

Canonical skill/agent prefix: [execution-context.md](execution-context.md).

Completion push (OS toast / webhook) is companion-only, default **off**, at-most-once
attempt after terminal live runs (hardened durable `runs/<id>`). Prefer
`setup --notification-mode auto` for background jobs. Never status/jobs alone.

**Not in 1.5.0 (PR5 → 1.7.0):** operator re-attempt; direct-mode push notify;
headless/native honesty polish (setup/docs). Native toasts need a **macOS/Linux
desktop session** today; they are **not** implemented on Windows. Use
`webhook` for SSH/CI/**Windows** (and any headless host) until PR5 docs/setup
polish; Windows toast stays out until a smoke-test host exists.

## Optional stop-review gate

Off by default. Enable with `/grok:setup --enable-review-gate`. See
`hooks/hooks.json` and `scripts/stop-review-gate-hook.mjs`.

## Manual smoke

See `manual-smoke.md`.
