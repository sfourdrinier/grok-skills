<!-- plugin/references/execution-context.md -->

# Execution context for notifications (single pattern)

**Canonical prefix** - every skill/agent that can run a live mode must set this
**before** `node …/run.mjs` or `node …/agents/run.mjs`. Do not invent variants.

```bash
# Foreground (wait for results in this turn):
export GROK_COMPANION_EXECUTION_CONTEXT=foreground

# Background (host runs companion in a background task):
export GROK_COMPANION_EXECUTION_CONTEXT=background
```

Then invoke the runner as usual, for example:

```bash
export GROK_COMPANION_EXECUTION_CONTEXT=background
node "$SKILL_BASE/run.mjs" review --target '.' --task-file - <<'GROK_TASK'
…
GROK_TASK
```

Rules (design §11 / PR3; 2.0.1+):

- Values: only `foreground` or `background`.
- Precedence: env `GROK_COMPANION_EXECUTION_CONTEXT` > companion flag
  `--execution-context foreground|background` > **auto-detect** when unset
  (non-TTY stdout → `background`, TTY → `foreground`).
- Skills/agents should still **export explicitly** in fenced commands for
  predictable notify behavior; auto-detect is a footgun fix for piped hosts.
- Companion uses this for `notificationMode=auto` (notify only when
  **background**). New installs default notification mode to **`auto`**.
- Companion **never** forwards this env to the Python wrapper.
- Do **not** change `skill-run.mjs`; set the env in the skill/agent shell only.
- Completion notify is mode-gated: only
  `review` / `reason` / `code` / `verify` / `adversarial-review`
  (`NOTIFY_ELIGIBLE_MODES`). Peer-stop / handoff / status / setup / etc. are
  **not** eligible even when notifications are on.
