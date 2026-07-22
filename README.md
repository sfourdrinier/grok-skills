# grok-skills

Run [Grok](https://x.ai) from Claude Code or Codex (the ChatGPT desktop coding surface) as a second pair of hands: review, reason, implement directly in your tree (default) or in an isolated worktree, verify. Not affiliated with xAI.

Works on whatever repo you point it at. The install location of this package is not the repo under review.

Plugin name: `grok`. Claude Code and Codex both install the same package; they
**invoke** skills differently (table below).

**Division of labor:** Claude Code or Codex = orchestrator. Grok (via this
plugin) = sandboxed second mind - especially **`grok-engineer-coder`** for
implementation directly in your tree (default) or in an isolated worktree.
How edits land is mode-aware:
[plugin/references/integration-modes.md](plugin/references/integration-modes.md).

---

## First 5 minutes

1. Install **Grok CLI**, log in, confirm `grok --version` works
   (macOS or Linux for live modes; Python 3 + Node on `PATH`). Any working Grok CLI
   build is accepted - there is no exact version lock.
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
3. Optional readiness check (agents already auto-install on SessionStart):

   ```text
   /grok:setup
   ```

   For **background completion signals** (OS toast / webhook when a job finishes
   while you are not watching), enable auto and run live skills in the background
   with the execution-context env set by the skill (see
   [execution-context.md](plugin/references/execution-context.md)):

   ```text
   /grok:setup --notification-mode auto
   ```

   Defaults stay **off** (no toast). Use `--notification-mode webhook` plus
   `--notification-webhook-url <https-url>` for SSH/CI/Windows (native toast is
   macOS/Linux desktop only). Full smoke:
   [manual-smoke.md](plugin/references/manual-smoke.md).

4. Before promising implementer success on the **live tree**, record integration
   consent or opt into isolation (canonical matrix:
   [integration-modes.md](plugin/references/integration-modes.md)):

   ```text
   /grok:setup --integration direct
   # or isolation without live land:
   /grok:setup --integration auto
   # or:
   /grok:setup --integration review
   ```

   First direct landing without setup consent fails closed. `/grok:implement`
   always forces an isolated worktree + verify-only handoff (never live lands).

5. Try a review or ask the host to use **grok-engineer-coder** for implementation.

You should see one JSON envelope on stdout with `"status": "success"` for live
modes. Use `/grok:jobs` / `/grok:result --pretty` (Claude) or the equivalent
skill names on Codex for later job output.

### Claude Code vs Codex: how you invoke things

| What | Claude Code | Codex (CLI / ChatGPT desktop) |
|------|-------------|-------------------------------|
| Install plugin | `/plugin marketplace add sfourdrinier/grok-skills` then `/plugin install grok@grok-skills` | `codex plugin marketplace add sfourdrinier/grok-skills` then `codex plugin add grok@grok-skills` |
| Skills | Slash commands **or** Skill tool: `/grok:review`, `/grok:code`, ... (model invocation enabled) | Skill picker / Skill tool - same skill **names** (`review`, `code`, `setup`, `dual-lens`, ...) |
| Subagents | Auto-loaded from plugin: `grok-engineer-coder`, `grok-rescue` | Auto-installed on SessionStart into `~/.codex/agents/` (or project `.codex/agents/` with `setup --codex-agents-scope project`; absolute `agents/run.mjs`) |
| Implement with Grok | Spawn **grok-engineer-coder**, or `/grok:code` | Spawn **grok-engineer-coder** (nickname **Grok Coder**), or run **code** skill |
| Stop / SubagentStop hooks | Claude hooks (stop gate + mode-aware handoff nudge) | **Dormant by default** until trusted via `/hooks` (see Codex trust note below) |

Same engine either way: Node companion → hardened Python wrapper → one JSON envelope.

**Codex trust honesty:** on Codex, plugin hooks (the optional stop-review gate **and** the SubagentStop handoff nudge) stay **dormant until you trust them** via `/hooks`. That is the honest default posture - install alone does not enable those hooks. Skills and SessionStart agent materialization still work without hook trust. **SubagentStop is mode-aware:** peer never routes through `/grok:handoff`; code **direct** (edits already live) differs from worktree/auto handoff paths.

---

## How to use it

### Before anything else

You need all of these:

1. **macOS** (Seatbelt) or **Linux** (Landlock) for live hardened modes. Linux
   also needs **bubblewrap** (`bwrap` on `PATH`) and a Landlock-capable kernel
   (5.13+ with Landlock LSM). Windows still stops with `probe-required` until
   its sandbox is live-probed.
2. **Python 3** and **Node.js** on your `PATH` (stdlib only; no pip/npm packages for this tool).
3. **Grok CLI installed and logged in** (`grok --version` works). Any working
   Grok CLI is accepted. `plugin/wrapper/accepted-version.json` is **last
   maintainer-validated evidence only** (advisory) - not a runtime allowlist.

You do **not** need a manual clone for normal use. Claude Code and Codex both install from this GitHub repo as a **plugin marketplace** (they clone it, then copy `plugin/` into their install cache).

### Claude Code

From a Claude Code session (preferred - install straight from GitHub):

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
/grok:status --run-id <runId from code envelope>
/grok:handoff --run-id <runId>
# Integrate per mode (one-shot code): direct = edits already live; auto =
# companion may apply; review = parent applies after ready. ACP peer is always
# worktree-isolated and lands only at peer-stop (see integration-modes.md).
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

Preferred - marketplace from GitHub:

```bash
codex plugin marketplace add sfourdrinier/grok-skills
# optional pin: codex plugin marketplace add sfourdrinier/grok-skills --ref main
codex plugin add grok@grok-skills
codex plugin list   # expect grok@grok-skills installed, enabled
```

Also accepted: `https://github.com/sfourdrinier/grok-skills.git`, SSH URLs, or a local clone path for development.

Skills ship with the plugin. Invoke them the way your Codex build exposes plugin skills (skill picker / `$skill` style, depending on version). Prefer each skill’s self-locating `run.mjs` (Skill base directory); custom agents get an absolute `agents/run.mjs` path on SessionStart - do not invent cache paths by hand.

After install, start a new Codex session (or reload) so **SessionStart** can write managed `grok-*.toml` agents (default: `~/.codex/agents/`; or `<cwd>/.codex/agents/` after `setup --codex-agents-scope project`). Then spawn **grok-engineer-coder** / **grok-rescue**, or run skills the same way you would in Claude. Prefer tasks via `--task-file` / stdin heredoc so nothing shell-expands. On Codex, plugin hooks remain **dormant until trusted** via `/hooks` (stop gate and SubagentStop nudge); agent materialization does not require that trust step.

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

If the repository or your fork is private, git install only works for accounts that can clone it. Use a path or SSH remote you already have access to:

```bash
claude plugin marketplace add /absolute/path/to/grok-skills
codex plugin marketplace add git@github.com:sfourdrinier/grok-skills.git
```

### What each skill does

| Skill | What it does |
|-------|----------------|
| `/grok:preflight` | Readiness only: Grok CLI runnable (`grok --version`), auth, sandbox policy, private-home lifecycle. No task. No exact CLI build pin. |
| `/grok:setup` | Optional readiness + prefs (`--run-mode`, **`--notification-mode auto`** for background completion, **`--codex-agents-scope user\|project`**). Codex agents auto-install on SessionStart. |
| `/grok:review` | Read-only review. Target defaults to `.`; optional `--base` (framing only); opt-in `--isolated` for owned worktree snapshot. |
| `/grok:adversarial-review` | Hostile review that challenges design; web on by default. |
| `/grok:dual-lens` | Adversarial pass, then ordinary review on the same target. |
| `/grok:reason` | Cold second opinion on files you name. No automatic repo crawl. Web off by default. |
| `/grok:code` | Implements per **integration mode** (default **direct** = live tree; `auto`/`review` = external worktree off a committed `--base`). Does not commit or push. Optional `--contract-file` (writeScopes + requiredValidation; runMode hardened only). Handoff artifacts under the run dir for isolated modes. See [integration-modes.md](plugin/references/integration-modes.md). |
| `/grok:peer` | Multi-turn **ACP peer channel** (`start` / `prompt` / `stop`). Default path for `grok-engineer-coder`; one-shot `code` is the fallback (`GROK_DISABLE_ACP=1`). Hardened runMode only. **Always** external retained worktree during the session (not live-edit). At ready `peer stop`, `direct`/`auto` apply the verified patch (direct needs consent); `review` retains. Shared auto/peer apply spine; **not** via `/grok:handoff`. Final apply envelope rewrite-before-write/store/finalize; **not** completion-notification eligible. Does **not** claim host-level tool-approval enforcement beyond local CLI parse+initialize probe - trusted-input peer channel. See [peer skill](plugin/skills/peer/SKILL.md) + [integration-modes.md](plugin/references/integration-modes.md). |
| `/grok:implement` | **One-call delegate:** `code` then auto-`handoff` on the resulting runId. Relays both envelopes. Exit 0 only when code ok AND handoff dual-condition ready. Hardened runMode only (runMode direct refused). **Always** isolated worktree + verify-only (never live lands even when workspace is direct/auto); for apply-on-ready use `code --integration auto`. |
| `/grok:handoff` | **Read-only** verified implementation handoff by **`runId` only** (1.6.0+). Dual-condition ready: ready manifest + success envelope + patch rehash. Never applies (read-only). Code-mode only (peer runIds refuse). Notify is not ready. |
| `/grok:verify` | Pass/fail/inconclusive check on an existing worktree. No `--web`. |
| `/grok:debate` | Two opposing Grok reason passes + synthesis on a topic. |
| `/grok:status` | Jobs table, or read-only wrapper status with `--run-id` (or a bare runId - the companion translates; lifecycle projection; exit 1 can mean a failed target). |
| `/grok:jobs` | List recent companion-tracked jobs. |
| `/grok:result` | Stored job output (`--pretty` for Markdown). Accepts a job id or a runId - the companion translates. |
| `/grok:cancel` | Cancel a running job. Accepts a job id or a runId - the companion translates. |
| `/grok:transfer` | Package Claude session context into a Grok task pack. |
| `/grok:cleanup` | Dry-run by default; `--confirm` removes owned run state / worktree. |

### Agents (orchestrator host + Grok worker)

| Agent | Role |
|-------|------|
| **`grok-engineer-coder`** | Prefer for implementation: features, fixes, refactors. Default multi-turn ACP peer (always external worktree; stop-time apply for direct/auto); one-shot `code` fallback (default live tree; opt-in auto/review worktrees). See [integration modes](plugin/references/integration-modes.md). Host plans/merges; Grok writes. |
| **`grok-rescue`** | Second opinion / diagnosis via Grok `reason` (or `code` if target+base are already known). |

- **Claude Code:** agents ship in the plugin (`plugin/agents/`). Reload plugins after install.
- **Codex:** agents auto-install on **SessionStart** with an absolute
  `GROK_AGENT_RUN` → `agents/run.mjs` (Codex cannot register plugin agents
  natively yet - [openai/codex#18988](https://github.com/openai/codex/issues/18988)).
  Default dest is personal `~/.codex/agents/`; `setup --codex-agents-scope project`
  persists workspace prefs and installs into `<cwd>/.codex/agents/` instead
  (SessionStart honors the same prefs; see
  [Codex subagents](https://developers.openai.com/codex/subagents)).
  Managed files refresh on plugin upgrade (at most 3 managed `*.bak*` kept;
  user-owned TOML is never pruned or overwritten unless `setup --force-codex-agents`).
  If agents are missing after install, open a **new session** or run optional
  `setup --force-codex-agents` (hook failures are non-blocking so they never stall
  host startup). Nicknames: **Grok Coder** / **Grok Rescue**.
- **Codex hooks stay dormant until trusted:** the stop-review gate and the
  mode-aware SubagentStop handoff nudge are skipped on Codex until you approve
  them via `/hooks` (peer never via handoff; code direct vs worktree handoff
  differ). Skills and agent materialization do not depend on that trust step.
- **Transparent skills + agents:** skills use `$SKILL_BASE/run.mjs`; Claude/Codex
  agents use `agents/run.mjs` (self-locating). See
  [plugin-root.md](plugin/references/plugin-root.md).
- **Remove managed Codex agents:** disable/uninstall the plugin first, then
  `setup --remove-codex-agents` (or delete managed `grok-*.toml` under the active
  scope dir).


### Run modes (security posture)

Two postures, same skills. This is the **security** axis (`runMode`), not how
edits land - both axes use the word "direct"; disambiguate in
[integration-modes.md](plugin/references/integration-modes.md).

| Mode | How | What you get | Handoff artifacts |
|------|-----|----------------|-------------------|
| **hardened** (default) | omit, or `/grok:setup` with `--run-mode hardened` | Private Grok home, sandbox verification, secret redaction; isolation depends on channel + **integration** (one-shot code: worktree for auto/review, live tree for code direct; ACP peer: always external worktree, stop-time apply for direct/auto). | **Yes** for isolated paths (code auto/review and all ACP peer sessions) - verified patch + handoff manifest under the run dir. |
| **direct** | `GROK_SKILLS_MODE=direct` or companion `setup --run-mode direct` | Uses your **installed Grok CLI** and normal `~/.grok` auth - same idea as OpenAI's plugin using your installed Codex. Faster, less isolation. runMode direct does **not** push completion notify in 1.5.0 (job still tracked). `result` accepts `direct-<timestamp>` ids; direct runs are **synchronous** (the companion blocks in the CLI call and records no live child pid), so `cancel` has no process to signal, and `handoff`/`status --run-id`/`implement` refuse with an honest message. | **No** - by design: handoff artifacts' value is the isolation evidence (worktree, sentinel, sandbox verification) that runMode direct cannot attest. |

### Integration modes (how edits land)

Orthogonal to run mode (security). Default product name is **direct**. For
**one-shot code** that means edit this working tree under hardened-direct;
the first direct run without recorded consent fails closed with a trust
summary. **`/grok:implement` always forces an isolated worktree + verify-only
handoff** and never lands live (even when the workspace default is
direct/auto) - see [integration-modes.md](plugin/references/integration-modes.md).
**ACP peer** always uses an external worktree and only applies at ready
peer-stop (`direct` still needs that same consent; `auto` applies without it;
`review` retains). Accept once:

```bash
node "$SKILL_BASE/run.mjs" setup --integration direct
```

Or opt into isolation: `--integration auto` (worktree + apply-on-ready) or
`--integration review` / `worktree` (worktree + manual parent apply). Settings
`userConfig.integrationMode` / env `CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE` are a
default hint only - they do **not** satisfy consent; only setup does.

**Canonical matrix (modes, honesty, two "direct" axes, ACP):**
[plugin/references/integration-modes.md](plugin/references/integration-modes.md).

On Claude Code, plugin `userConfig` (Settings) can also set the default run mode,
integration mode, and notification prefs. The host exports them as
`CLAUDE_PLUGIN_OPTION_*` env vars. Precedence: `/grok:setup` workspace prefs >
`userConfig` env > built-in defaults (invalid env values are ignored). Job state
prefers absolute `CLAUDE_PLUGIN_DATA` when the host provides it. Details:
[plugin/references/README.md](plugin/references/README.md).

```bash
# Prefer skill runner after Skill tool load:
node "$SKILL_BASE/run.mjs" setup --run-mode direct
node "$SKILL_BASE/run.mjs" setup --run-mode hardened
node "$SKILL_BASE/run.mjs" setup --integration direct
# Or from a known install:
node "${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}/scripts/grok-companion.mjs" setup --run-mode hardened
```

### Implementation handoff (1.6.0+) - peer multi-agent integrate API

After a hardened `/grok:code` run (with or without `--contract-file`), the wrapper
writes under the state root:

- `runs/<runId>/artifacts/implementation.patch` (immutable git binary full-index)
- `runs/<runId>/implementation-handoff.json`

**Parents (Claude Code / Codex) must call `/grok:handoff --run-id <runId>` before
integrating isolated (auto/review) results.** Dual-condition ready is true only
when the manifest says ready, a **success** terminal envelope exists for that
runId, and the patch re-hashes. Completion **notifications are not ready** -
they only mean a terminal attempt finished.

Integrate is **mode-aware** and **channel-aware** (canonical:
[integration-modes.md](plugin/references/integration-modes.md)):

- **code direct:** source edits already live in your tree (protected paths
  rolled back if touched); no patch gate required for the edit to exist
- **code auto:** companion may auto-apply a dual-condition-ready patch after
  apply-time revalidation (patch integrity + shared dirty-guard spine)
- **code review:** never auto-applies; parent apply is manual (`git apply
  --check --binary` then explicit apply)
- **ACP peer:** always external worktree during the session; at ready
  `peer stop`, `direct`/`auto` apply the verified patch (direct needs consent),
  `review` retains; handoff refuses peer runIds

This plugin **never** auto-commits, merges, cherry-picks, or pushes in any mode.
Handoff checklist: [implementation-handoff.md](plugin/references/implementation-handoff.md).

`--contract-file` is operator-trusted (`operator-contract-trusted-no-os-sandbox`):
argv arrays only, cwd confined to the worktree, **no** OS filesystem sandbox claim.
runMode direct (`setup --run-mode direct`) **rejects** `--contract-file` (fail closed);
handoff/status/cleanup always use the hardened wrapper even if prefs say direct.

### Completion notifications (optional, 1.5.0+)

Default is **off**. Turn on for background jobs:

```text
/grok:setup --notification-mode auto
```

| Mode | When you get a signal |
|------|------------------------|
| `off` (default) | Never |
| `auto` | Native toast only if the skill ran with `GROK_COMPANION_EXECUTION_CONTEXT=background` |
| `native` | OS toast on macOS/Linux desktop (FG or BG) |
| `webhook` | POST JSON if you also set `--notification-webhook-url https://...` |

Skills set the execution-context env in their bash fences (foreground vs
background). You do not invent that env by hand for normal slash use; for a
**background** host task, the skill path uses `background` so `auto` can fire.
At-most-once attempt only (`runs/<runId>/notified.json`); details:
[execution-context.md](plugin/references/execution-context.md),
[manual-smoke.md](plugin/references/manual-smoke.md).

### Useful flags (live modes)

- Exactly one of `--task '…'` or `--task-file path` (prefer a file for long prompts).
- `--web` only on review / reason / code when you need live docs or current APIs. Off by default. Never on verify.
- `--model` (default `grok-4.5`), `--timeout` (mode-dependent; often 900s), optional
  `--max-turns` (**omit for unlimited** - default). Defaults live in
  [wrapper/SKILL.md](plugin/wrapper/SKILL.md) and argparse; skills pass flags through.
  Incomplete Cancelled/turn-cap stops with real findings still return success + warning
  (see [COMPATIBILITY.md](docs/COMPATIBILITY.md)).

### Reading the result

Every run prints **exactly one JSON envelope** on stdout (success, failure, or
in-flight `running`). Exit code is 0 when `"status"` is `"success"` or
`"running"`; otherwise 1. For `/grok:status`, exit 1 can mean a successfully
inspected failed/interrupted **target** - still relay the JSON. Treat the
envelope as the source of truth; any prose after it is optional commentary.

For `code`, look for `worktreePath` / `changedFiles` in the envelope. For `verify`, look for the structured verdict.

### Direct wrapper (no plugin)

Same engine the plugin shells to. **Defaults differ from the product companion:**
the companion/skills default to **integration=direct** after per-repo setup
consent; a bare wrapper `code` call without `--integration` intentionally
defaults to **worktree** (fail-closed isolation) so an un-consented bare call
cannot silently edit the live tree. Pass `--integration direct` only when you
mean the live-tree posture.

```bash
python3 plugin/wrapper/scripts/grok_agent.py preflight
python3 plugin/wrapper/scripts/grok_agent.py review \
  --target src/my-lib \
  --task-file task.md
# bare code: worktree isolation by default
python3 plugin/wrapper/scripts/grok_agent.py code \
  --target src/my-lib --base HEAD --task-file task.md
# explicit live-tree (same as product after consent):
python3 plugin/wrapper/scripts/grok_agent.py code \
  --integration direct --target src/my-lib --task-file task.md
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
- **Mode-aware write landing** (see [integration-modes.md](plugin/references/integration-modes.md)):
  - **One-shot `code` `integration=direct`** (product default after per-repo consent):
    live-tree edits under hardened-direct (private home + sandbox write-confined to
    the **repo root** + private tmp + post-run protected-path guards). Not worktree
    isolation.
  - **One-shot `code` `auto` / `review` / `worktree`**: external worktree + escape
    checks; auto may apply a verified ready patch; review retains for parent apply.
  - **Bare wrapper** `python3 …/grok_agent.py code` without `--integration`: still
    defaults to **worktree** so an un-consented bare call cannot silently edit the
    live tree.
  - **ACP peer:** always external retained worktree during the session; at ready
    peer-stop, `direct`/`auto` apply (direct needs consent), `review` retains.
    Peer direct is stop-time apply, not one-shot code live-edit.
- One redacted JSON envelope on stdout (pattern scan + exact values from the injected `auth.json`)
- Build scripts that Grok rewrote are not executed (gate refused)

What it does not do:

- Block absolute-path **reads** of host secrets on the current Grok CLI
- Block network egress (Grok is online by design)
- Guarantee pattern redaction catches every secret shape
- Replace host tool-approval UX (Claude/Codex still prompt for Bash as usual)

**Model invocation:** skills allow host models to invoke them without a slash
command (`disable-model-invocation` is not set). That is intentional for dual-host
Skill-tool use; treat this plugin as trusted input for those hosts.

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
| “Could not locate the Grok wrapper” | Reinstall the plugin from this repo. Confirm the cache (or `--plugin-dir`) contains `wrapper/scripts/grok_agent.py`. Advanced only: `GROK_AGENT_WRAPPER` + `GROK_ALLOW_WRAPPER_OVERRIDE=1`. |
| `version-mismatch` | `grok --version` failed, exited nonzero, or did not print a usable Grok version line. Fix/install the Grok CLI; the plugin does **not** require a specific CLI build. |
| Auth / login checks fail in preflight | Log in with the Grok CLI itself, then re-run `/grok:preflight`. |
| `probe-required` on Windows | Expected until Windows sandbox is live-probed. |
| `probe-required` on Linux without `bwrap` | Install bubblewrap (`bwrap` on PATH). Required pre-spawn prereq; secret-read denial remains unproven even with bwrap (D-SECRETREAD). Landlock write confinement is verified after each live run via ProfileApplied. |
| Skills missing after install | Claude: `/reload-plugins`. Codex: check `codex plugin list`. Desktop: restart after install. |
| Codex install: which name? | Use `grok@grok-skills` (plugin@marketplace). |
| Codex agents missing from picker | Open a **new session** after install (SessionStart installs them). Confirm `~/.codex/agents/grok-*.toml` exist and `GROK_AGENT_RUN` / `# agent-run:` point at the current `agents/run.mjs`. Re-run optional `/grok:setup` or `setup --force-codex-agents` if you customized those files. |
| Codex agent: `plugin root not set` | Stale agent from pre-1.2.5. New session or `setup --force-codex-agents` rewrites absolute `agents/run.mjs` path. |
| Mixed / stale plugin after upgrade | `run.mjs`, companion, and SessionStart force the install tree they live in. Prefer Skill base + `run.mjs`; open a new session after upgrade. |
| Model invents wrong cache paths | Ignore invented paths. See [plugin-root.md](plugin/references/plugin-root.md). |
| Review/code ends Cancelled mid-work | With findings: `status` may be success + `incompleteStop: true` and **exit 1** (not done). Empty cancel fails as `cancelled`. Resume with hardened `--continue-run <runId>` (not `direct-*`). |
| `--continue-run direct-…` refused | Expected: runMode=direct has no continuable state. Use hardened run ids under `~/.local/state/grok-skills/runs/`. |
| Want managed Codex agents gone | Disable/uninstall plugin first (SessionStart reinstalls while enabled), then `setup --remove-codex-agents`. |
| Review notes files changed during the run | Informational only (dev servers, logs, other editors, or Grok listing paths). Review still **succeeds**; findings apply. Not a failure. See [over-conservatism audit](docs/reviews/2026-07-15-over-conservatism-audit.md). |
| Warning: "AGENTS.md and CLAUDE.md differ at ..." | Informational (2.0.0+). Both files exist at that level with different bodies (comparison ignores the first/header line, matching `ruleFileParity`) and only AGENTS.md was sent to Grok. Pointer-style CLAUDE.md (`@AGENTS.md`, optionally with surrounding whitespace) never warns. Align the pair, or set `"ruleFileParity": true` in `.grok-skills.json` to enforce matching pairs fail-closed. |
| `code` fails `unexpected-edits` naming files you changed yourself | Do not commit or edit the target checkout while a hardened `code` run is in flight; the original-checkout guard cannot attribute mid-run divergence. Rerun the task, then integrate in a quiet window. |
| No completion toast after a long job | Notifications default **off**. Run `/grok:setup --notification-mode auto` and use a **background** skill run (execution context). Headless/Windows: use `webhook`. Direct mode has no push notify in 1.5.0. Peer-stop is not notify-eligible. |
| Toast arrived but cannot integrate | Notify is only a signal. Call `/grok:handoff --run-id` and require dual-condition ready before any apply. |
| `--contract-file` in direct mode | Fail closed. Use hardened mode (`setup --run-mode hardened`) for contracts and handoff artifacts. |

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
