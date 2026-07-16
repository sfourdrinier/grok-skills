# grok-skills

Run [Grok](https://x.ai) from Claude Code or Codex (the ChatGPT desktop coding surface) as a second pair of hands: review, reason, implement in an isolated worktree, verify. Not affiliated with xAI.

Works on whatever repo you point it at. The install location of this package is not the repo under review.

Plugin name: `grok`. Claude Code and Codex both install the same package; they
**invoke** skills differently (table below).

**Division of labor:** Claude Code or Codex = orchestrator. Grok (via this
plugin) = sandboxed second mind — especially **`grok-engineer-coder`** for
implementation in an isolated worktree.

---

## First 5 minutes

1. Install **Grok CLI**, log in, confirm `grok --version` matches
   [`plugin/wrapper/accepted-version.json`](plugin/wrapper/accepted-version.json)
   (macOS for live modes; Python 3 + Node on `PATH`).
2. Add the marketplace and install the plugin (no manual clone needed):

   ```text
   # Claude Code (in-session or CLI)
   /plugin marketplace add sfourdrinier/grok-skills
   /plugin install grok@grok-skills
   ```

   ```bash
   # Codex CLI
   codex plugin marketplace add sfourdrinier/grok-skills
   codex plugin add grok@grok-skills
   ```

   Full URL `https://github.com/sfourdrinier/grok-skills.git` works too; GitHub
   shorthand is equivalent. Then `/reload-plugins` (Claude) or start a new session
   (Codex agents materialize on **SessionStart** - no manual setup).
3. Optional readiness check (gate/mode toggles only; agents already auto-install):

   ```text
   /grok:setup
   ```

4. Try a review or ask the host to use **grok-engineer-coder** for implementation.

You should see one JSON envelope on stdout with `"status": "success"` for live
modes. Use `/grok:jobs` / `/grok:result --pretty` (Claude) or the equivalent
skill names on Codex for later job output.

### Claude Code vs Codex: how you invoke things

| What | Claude Code | Codex (CLI / ChatGPT desktop) |
|------|-------------|-------------------------------|
| Install plugin | `/plugin marketplace add sfourdrinier/grok-skills` then `/plugin install grok@grok-skills` | `codex plugin marketplace add sfourdrinier/grok-skills` then `codex plugin add grok@grok-skills` |
| Skills | Slash commands **or** Skill tool: `/grok:review`, `/grok:code`, ... (model invocation enabled) | Skill picker / Skill tool - same skill **names** (`review`, `code`, `setup`, `dual-lens`, ...) |
| Subagents | Auto-loaded from plugin: `grok-engineer-coder`, `grok-rescue` | Auto-installed on SessionStart into `~/.codex/agents/` (absolute companion path) |
| Implement with Grok | Spawn **grok-engineer-coder**, or `/grok:code` | Spawn **grok-engineer-coder**, or run **code** skill |
| Stop gate hooks | Claude hooks | Same hooks; may require **trust** via `/hooks` |

Same engine either way: Node companion → hardened Python wrapper → one JSON envelope.

---

## How to use it

### Before anything else

You need all of these:

1. **macOS** for live modes (Seatbelt). Linux/Windows stop with `probe-required` until a sandbox profile is validated for them.
2. **Python 3** and **Node.js** on your `PATH` (stdlib only; no pip/npm packages for this tool).
3. **Grok CLI installed and logged in** (`grok --version` works). This project pins a known-good build in `plugin/wrapper/accepted-version.json`. If your CLI version does not match, the wrapper refuses to run until you revalidate (see [CONTRIBUTING.md](CONTRIBUTING.md)).

You do **not** need a manual clone for normal use. Claude Code and Codex both install from this GitHub repo as a **plugin marketplace** (they clone it, then copy `plugin/` into their install cache).

### Claude Code

From a Claude Code session (preferred — install straight from GitHub):

```text
/plugin marketplace add sfourdrinier/grok-skills
/plugin install grok@grok-skills
```

CLI equivalent:

```bash
claude plugin marketplace add sfourdrinier/grok-skills
claude plugin install grok@grok-skills
```

Other accepted sources: full git URL (`https://github.com/sfourdrinier/grok-skills.git`), or pin a ref with `sfourdrinier/grok-skills@main`. Then `/reload-plugins` (or restart). Type `/grok:` and confirm autocomplete.

Typical session:

```text
/grok:preflight
/grok:review --target src/my-lib --task "Find correctness bugs and unsafe error handling"
/grok:code --target src/my-lib --base main --task "Fix the off-by-one in the paginator"
/grok:verify --worktree /path/to/retained-worktree --task "Confirm the fix builds and tests pass"
```

Local path is only for hacking on a checkout:

```bash
# marketplace from a clone
claude plugin marketplace add /absolute/path/to/grok-skills
claude plugin install grok@grok-skills

# or load the plugin tree without a marketplace
claude --plugin-dir /absolute/path/to/grok-skills/plugin
```

You do **not** need `GROK_AGENT_WRAPPER` for a normal install. The engine lives inside the plugin tree (`plugin/wrapper/…`), so the install cache still finds it.

Full interactive checklist: [plugin/references/manual-smoke.md](plugin/references/manual-smoke.md).

### Codex CLI

Preferred — marketplace from GitHub:

```bash
codex plugin marketplace add sfourdrinier/grok-skills
# optional pin: codex plugin marketplace add sfourdrinier/grok-skills --ref main
codex plugin add grok@grok-skills
codex plugin list   # expect grok@grok-skills installed, enabled
```

Also accepted: `https://github.com/sfourdrinier/grok-skills.git`, SSH URLs, or a local clone path for development.

Skills ship with the plugin. Invoke them the way your Codex build exposes plugin skills (skill picker / `$skill` style, depending on version). Skills use `PLUGIN_ROOT` from the install; custom agents get an **absolute companion path** on SessionStart - do not invent cache paths by hand.

After install, start a new Codex session (or reload) so **SessionStart** can write `~/.codex/agents/grok-*.toml`. Then spawn **grok-engineer-coder** / **grok-rescue**, or run skills the same way you would in Claude. Prefer tasks via `--task-file` / stdin heredoc so nothing shell-expands.

### ChatGPT desktop (Codex)

Same package as the CLI (marketplace name `grok-skills`, plugin `grok`).

1. Prefer adding the marketplace from git the same way as Codex CLI
   (`sfourdrinier/grok-skills` or the HTTPS URL).
2. Open **Plugins** → **Grok Skills** marketplace → install **grok**.
3. Restart / open a new session so SessionStart can install Codex agents.
   Trust hooks only if you enable the optional stop gate (`/hooks` in CLI).
   Leave the gate off unless you want that. No separate setup skill is required.

If the desktop build only offers “open as project,” open a clone of this repo once
so it discovers `.agents/plugins/marketplace.json`, then install **grok** from there.
CLI path is always available: `codex plugin marketplace add sfourdrinier/grok-skills`
then `codex plugin add grok@grok-skills`.

### Private repo / no public access

While this repository is private (or if you fork it), git install only works for accounts that can clone it. Use a path or SSH remote you already have access to:

```bash
claude plugin marketplace add /absolute/path/to/grok-skills
codex plugin marketplace add git@github.com:sfourdrinier/grok-skills.git
```

### What each skill does

| Skill | What it does |
|-------|----------------|
| `/grok:preflight` | Readiness only: binary pin, auth, sandbox policy, private-home lifecycle. No task. |
| `/grok:setup` | Optional readiness report + gate/mode toggles. Codex agents auto-install on SessionStart. |
| `/grok:review` | Read-only review. Target defaults to `.`; optional `--base` for branch review. |
| `/grok:adversarial-review` | Hostile review that challenges design; web on by default. |
| `/grok:dual-lens` | Adversarial pass, then ordinary review on the same target. |
| `/grok:reason` | Cold second opinion on files you name. No automatic repo crawl. Web off by default. |
| `/grok:code` | Implements in an **external git worktree** off a committed `--base`. Does not commit or push. |
| `/grok:verify` | Pass/fail/inconclusive check on an existing worktree. No `--web`. |
| `/grok:debate` | Two opposing Grok reason passes + synthesis on a topic. |
| `/grok:status` | Jobs table, or wrapper status with `--run-id`. |
| `/grok:jobs` | List recent companion-tracked jobs. |
| `/grok:result` | Stored job output (`--pretty` for Markdown). |
| `/grok:cancel` | Cancel a running job by id. |
| `/grok:transfer` | Package Claude session context into a Grok task pack. |
| `/grok:cleanup` | Dry-run by default; `--confirm` removes owned run state / worktree. |

### Agents (orchestrator host + Grok worker)

| Agent | Role |
|-------|------|
| **`grok-engineer-coder`** | Prefer for implementation: features, fixes, refactors. Runs Grok `code` in an isolated worktree (optional `verify`). Host plans/merges; Grok writes. |
| **`grok-rescue`** | Second opinion / diagnosis via Grok `reason` (or `code` if target+base are already known). |

- **Claude Code:** agents ship in the plugin (`plugin/agents/`). Reload plugins after install.
- **Codex:** agents auto-install on **SessionStart** into `~/.codex/agents/` with an
  absolute path to `grok-companion.mjs` (Codex cannot register plugin agents natively
  yet - [openai/codex#18988](https://github.com/openai/codex/issues/18988)). Managed
  files refresh on plugin upgrade (with `*.bak`); user-owned TOML is left alone unless
  `setup --force-codex-agents`.
- **Transparent skills + agents:** skills use `$SKILL_BASE/run.mjs`; Claude/Codex
  agents use `agents/run.mjs` (self-locating). See
  [plugin-root.md](plugin/references/plugin-root.md).
- **Remove managed Codex agents:** disable/uninstall the plugin first, then
  `setup --remove-codex-agents` (or delete managed `~/.codex/agents/grok-*.toml`).


### Run modes (security posture)

Two postures, same skills:

| Mode | How | What you get |
|------|-----|----------------|
| **hardened** (default) | omit, or `/grok:setup` with `--run-mode hardened` | Private Grok home, sandbox verification, worktree isolation, secret redaction. |
| **direct** | `GROK_SKILLS_MODE=direct` or companion `setup --run-mode direct` | Uses your **installed Grok CLI** and normal `~/.grok` auth — same idea as OpenAI's plugin using your installed Codex. Faster, less isolation. |

```bash
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --run-mode direct
node "${GROK_PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --run-mode hardened
```

### Useful flags (live modes)

- Exactly one of `--task '…'` or `--task-file path` (prefer a file for long prompts).
- `--web` only on review / reason / code when you need live docs or current APIs. Off by default. Never on verify.
- `--model`, `--timeout`, `--max-turns` if you need them; defaults are in the skill docs under `plugin/skills/`.

### Reading the result

Every run prints **exactly one JSON envelope** on stdout (success or failure). Exit code is 0 only when `"status": "success"`. Treat that envelope as the source of truth; any prose after it is optional commentary.

For `code`, look for `worktreePath` / `changedFiles` in the envelope. For `verify`, look for the structured verdict.

### Direct wrapper (no plugin)

Same engine the plugin shells to:

```bash
python3 plugin/wrapper/scripts/grok_agent.py preflight
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target src/my-lib \
  --task-file task.md
```

---

## Optional project config

No config required. For JS monorepos that need overrides, put `.grok-skills.json` at the **target repo** root (not in this package):

```json
{
  "packageManager": "pnpm",
  "ruleFileParity": false,
  "neverBuildWorkspaces": {
    "@my/schemas": ["typecheck"],
    "@my/ui": ["typecheck", "lint"]
  }
}
```

- `packageManager`: `pnpm` / `npm` / `yarn` / `bun`, or `null` to skip the JS build gate.
- `neverBuildWorkspaces`: run listed scripts instead of `build` for named packages.
- `ruleFileParity`: when `true`, require matched AGENTS.md/CLAUDE.md pairs. Default is off (single CLAUDE.md is fine).

Non-JS repos skip the JS package-manager gate with a warning; review/reason/code/verify still work.

---

## Security (short version)

This is a **trusted-input** tool for repos you are willing to let Grok read (and, in `code`/`verify`, run build/test scripts against). It is not a jail for a hostile model.

What it actually enforces:

- Private throwaway Grok home per run (your real credentials are not the run’s `HOME`)
- OS sandbox write confinement on the supported platform (verified after the run)
- `code` only writes inside an external worktree + escape checks
- One redacted JSON envelope on stdout (pattern scan + exact values from the injected `auth.json`)
- Build scripts that Grok rewrote are not executed (gate refused)

What it does not do:

- Block absolute-path **reads** of host secrets on the pinned Grok CLI
- Block network egress (Grok is online by design)
- Guarantee pattern redaction catches every secret shape

More: [SECURITY.md](SECURITY.md), [docs/OPEN-SECURITY-DECISIONS.md](docs/OPEN-SECURITY-DECISIONS.md).

---

## Layout

```
grok-skills/
  .claude-plugin/marketplace.json    # Claude Code marketplace
  .agents/plugins/marketplace.json   # Codex / ChatGPT marketplace
  plugin/                            # install unit (cache-safe)
    skills/                          # /grok:* definitions
    scripts/                         # companion, gate, relay, SessionStart
    wrapper/                         # Python engine (bundled)
    agents/                          # Claude: grok-engineer-coder, grok-rescue
    codex-agents/                    # Codex TOML templates (auto -> ~/.codex/agents)
    hooks/                           # SessionStart agent ensure + optional stop gate
    assets/
  docs/                              # security, provenance, compatibility
```

Compatibility notes and versions tested: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

---

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| “Could not locate the Grok wrapper” | Reinstall the plugin from this repo. Confirm the cache (or `--plugin-dir`) contains `wrapper/scripts/grok_agent.py`. Only set `GROK_AGENT_WRAPPER` if you moved the binary on purpose. |
| `version-mismatch` | Your `grok --version` does not match `plugin/wrapper/accepted-version.json`. Revalidate or install the pinned build ([cli-reference](plugin/wrapper/references/cli-reference.md)). |
| Auth / login checks fail in preflight | Log in with the Grok CLI itself, then re-run `/grok:preflight`. |
| `probe-required` on Linux/Windows | Expected until that platform’s sandbox is live-probed. |
| Skills missing after install | Claude: `/reload-plugins`. Codex: check `codex plugin list`. Desktop: restart after install. |
| Codex install: which name? | Use `grok@grok-skills` (plugin@marketplace). |
| Codex agents missing from picker | Open a **new session** after install (SessionStart installs them). Confirm `~/.codex/agents/grok-*.toml` exist and `companion:` points at the current plugin cache. Re-run optional `/grok:setup` or `setup --force-codex-agents` if you customized those files. |
| Codex agent: `plugin root not set` | Stale agent from pre-1.2.1. New session or `setup --force-codex-agents` rewrites absolute companion path. |
| Model invents wrong cache paths | Ignore invented paths. See [plugin-root.md](plugin/references/plugin-root.md). |
| Want managed Codex agents gone | Disable/uninstall plugin first (SessionStart reinstalls while enabled), then `setup --remove-codex-agents`. |
| Review notes files changed during the run | Informational only (dev servers, logs, other editors, or Grok listing paths). Review still **succeeds**; findings apply. Not a failure. See [over-conservatism audit](docs/reviews/2026-07-15-over-conservatism-audit.md). |

---

## Contributing

Tests, pin revalidation, and PR notes: [CONTRIBUTING.md](CONTRIBUTING.md).

**Cutting a release** (version bump, tests, tag, GitHub Release, post-smoke):
[docs/RELEASE.md](docs/RELEASE.md).

```bash
cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q
cd plugin/scripts && node --test tests/*.test.mjs
claude plugin validate ./plugin --strict
```

---

## Disclaimer

Not affiliated with, endorsed by, or sponsored by xAI. Grok is a trademark of xAI. This is an independent integration that calls the Grok CLI.
