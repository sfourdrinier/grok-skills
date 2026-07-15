<!-- plugin/references/manual-smoke.md -->

# grok plugin manual smoke checklist

Slash skills only fire inside a live Claude Code (or Codex skill) session. Run
this checklist once by hand after installing the plugin. Automated unit tests
already cover the companion, gate, and wrapper contracts.

## Preconditions

- Node and `python3` on PATH.
- Grok CLI installed, authenticated, matching `wrapper/accepted-version.json`.
- Plugin installed via marketplace (cache path) **or** `--plugin-dir ./plugin`.
- No `GROK_AGENT_WRAPPER` set (prove the bundled layout works).

## Install (Claude Code)

Preferred (GitHub marketplace):

1. `/plugin marketplace add sfourdrinier/grok-skills`
2. `/plugin install grok@grok-skills`
3. Reload plugins / restart session.
4. Confirm `/grok:` lists: preflight, setup, review, reason, code, verify,
   status, cleanup, jobs, result, cancel, transfer, debate, adversarial-review.
5. Optional unit tests from a clone:
   - `cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q`
   - `cd plugin/scripts && node --test tests/*.test.mjs`
   - `claude plugin validate ./plugin --strict`

Local checkout (dev only): `/plugin marketplace add /absolute/path/to/grok-skills`
then the same install step.

## Install (Codex)

Preferred:

1. `codex plugin marketplace add sfourdrinier/grok-skills`
2. `codex plugin add grok@grok-skills` (or install from the app plugin directory)
3. Start a **new session** (SessionStart auto-installs agents - no setup skill required).
4. Confirm `~/.codex/agents/grok-engineer-coder.toml` and `grok-rescue.toml` exist,
   each with `# managed-by: grok-skills` and a `companion:` absolute path under the
   current plugin cache.
5. Invoke preflight / review from the plugin skill surface; spawn engineer-coder once.

Local checkout (dev only): `codex plugin marketplace add /absolute/path/to/grok-skills`.

## Non-interactive engine smoke (no Claude UI)

```bash
export CLAUDE_PLUGIN_ROOT=/absolute/path/to/grok-skills/plugin
# Or only PLUGIN_ROOT for Codex-style env:
# export PLUGIN_ROOT=/absolute/path/to/grok-skills/plugin
node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs" preflight
```

Expect one JSON envelope with `mode: "preflight"` and `status: "success"` when
the Grok CLI is ready.

## Command checklist (Claude Code)

- [ ] `/grok:preflight` → one readiness envelope
- [ ] `/grok:setup` → optional readiness + gate status; enable/disable toggles work (not required for agents)
- [ ] `/grok:reason --task "Reply with exactly: PONG"` → success envelope
- [ ] `/grok:review --target . --task "list top risks"` → one review envelope
- [ ] `/grok:code --target . --base HEAD --task "trivial helper"` → worktree retained, no auto-commit
- [ ] `/grok:verify --worktree <path> --task "confirm tests"` → verifier verdict; `--web` refused
- [ ] `/grok:status --run-id <id>` → prior envelope
- [ ] `/grok:cleanup --run-id <id>` dry-run, then `--confirm`
- [ ] `grok-engineer-coder` routes implementation to companion `code` (one shell call)
- [ ] `grok-rescue` routes diagnosis to `reason` with one Bash call
- [ ] Codex: after SessionStart, `~/.codex/agents/grok-*.toml` present with absolute companion

## Cache-layout check (critical)

After a marketplace install, confirm the cached plugin contains:

```
<cache>/wrapper/scripts/grok_agent.py
<cache>/scripts/grok-companion.mjs
<cache>/skills/review/SKILL.md
```

If `wrapper/` is missing, the install is incomplete — reinstall from this repo.
