<!-- plugin/references/manual-smoke.md -->

# grok plugin manual smoke checklist

Slash skills only fire inside a live Claude Code (or Codex skill) session. Run
this checklist once by hand after installing the plugin. Automated unit tests
already cover the companion, gate, and wrapper contracts.

## Preconditions

- Node and `python3` on PATH.
- Grok CLI installed, authenticated (`grok --version` works; any build).
- Plugin installed via marketplace (cache path) **or** `--plugin-dir ./plugin`.
- No `GROK_AGENT_WRAPPER` set (prove the bundled layout works).

## Install (Claude Code)

Preferred (GitHub marketplace):

1. `/plugin marketplace add sfourdrinier/grok-skills`
2. `/plugin install grok@grok-skills`
3. Reload plugins / restart session.
4. Confirm `/grok:` lists: preflight, setup, review, reason, code, verify,
   handoff, status, cleanup, jobs, result, cancel, transfer, debate,
   adversarial-review.
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
   each with `# managed-by: grok-skills`, `# agent-run:`, and
   `GROK_AGENT_RUN=…/agents/run.mjs` under the current plugin cache
   (`# companion:` is optional metadata only).
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

## ACP peer channel (default; opt out with `GROK_DISABLE_ACP=1`)

The ACP peer channel is the default multi-turn peer path. Opt out (force
one-shot `code`) with `export GROK_DISABLE_ACP=1`. Spec:
`docs/specs/2026-07-17-acp-peer-channel-design.md` (Amendments supersede draft).
Peer-stop applies its verified patch itself per the active `--integration` mode
(review retains; auto/direct apply); it is not eligible for `/grok:handoff`.

Live smoke (start -> two prompts -> stop), recorded 2026-07-17
against grok 0.2.102 on a throwaway git repo (`note.txt` only):

```bash
export CLAUDE_PLUGIN_ROOT=/absolute/path/to/grok-skills/plugin
# peer-start (background resident wrapper; one running envelope). ACP is the
# default channel; pass --integration review to retain the patch for review.
node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs" peer start \
  --target . --base HEAD --integration review
# Capture runId + socketPath from the running envelope, then:
node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs" peer prompt \
  --run-id '<runId>' --task-file - <<'GROK_TASK'
Reply with exactly: PEER-PONG-1
GROK_TASK
node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs" peer prompt \
  --run-id '<runId>' --task-file - <<'GROK_TASK'
Reply with exactly: PEER-PONG-2
GROK_TASK
# peer-stop finalizes: real validation, then apply per --integration mode.
node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs" peer stop --run-id '<runId>'
```

Transcript tail (2026-07-17 live, runId `20260717T110823Z-140ae8`):

```text
start: running  peer={sessionId: 019f6fc3-..., socketPath: .../gs-.../.grok/p-140ae8.sock}
P1: success  result.stopReason=end_turn
P2: success  result.stopReason=end_turn
STOP: success peer-stop
  response.peer.confinement=worktree-final-diff-only
  response.peer.cleanup={status: clean}
  cleanup={status: clean}
```

Expect: start `status: running` with `response.peer.sessionId` + `socketPath`;
each prompt one redacted turn envelope; stop finalizes with
`implementation-handoff.json` carrying `confinement: "worktree-final-diff-only"`
(unless a scopes contract was supplied) and private home destroyed. With
`GROK_DISABLE_ACP=1`, the companion refuses peer modes with a one-line pointer
to the spec. Control socket lives under the private home (short AF_UNIX path),
not the run dir.

## Command checklist (Claude Code)

- [ ] `/grok:preflight` → one readiness envelope
- [ ] `/grok:setup` → optional readiness + gate status; enable/disable toggles work (not required for agents)
- [ ] `/grok:setup --notification-mode auto` → setup report shows notifications: auto
- [ ] `/grok:reason --task "Reply with exactly: PONG"` → success envelope
- [ ] `/grok:review --target . --task "list top risks"` → one review envelope (live checkout)
- [ ] `/grok:review --target . --isolated --task "list risks"` → isolation worktree cleaned after run
- [ ] `/grok:code --target . --base HEAD --task "trivial helper"` → worktree retained, no auto-commit
- [ ] Dual-host (Claude + Codex): after code, `/grok:status --run-id <id>` then
      `/grok:handoff --run-id <id>` → dual-condition ready only when success + patch
- [ ] Failed code / no changes → handoff ready false; tampered patch → integrity failure
- [ ] Notify does not replace handoff (integrate only after handoff ready)
- [ ] `/grok:verify --worktree <path> --task "confirm tests"` → verifier verdict; `--web` refused
- [ ] `/grok:status --run-id <id>` → prior envelope
- [ ] Background-style live run with `GROK_COMPANION_EXECUTION_CONTEXT=background` and
      notifications `auto` → `runs/<runId>/notified.json` may appear as `completed`
      (native may fail headless; marker still completes)
- [ ] `/grok:cleanup --run-id <id>` dry-run, then `--confirm`
- [ ] `grok-engineer-coder` routes implementation to companion `code` (one shell call; no unrestricted Bash)
- [ ] `grok-rescue` routes diagnosis to `reason` (not pure implement); one Bash(node) call
- [ ] Codex: after SessionStart, `~/.codex/agents/grok-*.toml` present with
      `# managed-by: grok-skills`, `# agent-run:`, and `GROK_AGENT_RUN=…/agents/run.mjs`
- [ ] Model does not invent `~/.claude/plugins/cache/...` paths (uses Skill base +
      `run.mjs`, or host env / managed `GROK_AGENT_RUN`)
- [ ] Optional: `setup --remove-codex-agents` removes managed agents only

## Cache-layout check (critical)

After a marketplace install, confirm the cached plugin contains:

```
<cache>/wrapper/scripts/grok_agent.py
<cache>/scripts/grok-companion.mjs
<cache>/skills/review/SKILL.md
```

If `wrapper/` is missing, the install is incomplete - reinstall from this repo.
