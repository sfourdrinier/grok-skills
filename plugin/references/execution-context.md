<!-- plugin/references/execution-context.md -->

# Execution context for notifications (single pattern)

**Canonical prefix** — every skill/agent that can run a live mode must set this
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

Rules (design §11 / PR3):

- Values: only `foreground` or `background`. Missing/invalid → treated as **foreground**.
- Companion uses this for `notificationMode=auto` (notify only when **background**).
- Companion **never** forwards this env to the Python wrapper.
- Do **not** change `skill-run.mjs`; set the env in the skill/agent shell only.
