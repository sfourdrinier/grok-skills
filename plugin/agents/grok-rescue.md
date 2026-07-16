---
name: grok-rescue
description: >
  Use when stuck and need a cold Grok second opinion or root-cause diagnosis via
  the hardened wrapper (reason). Prefer for investigation and plan critique - not
  for pure implementation (use grok-engineer-coder). May run code only if the user
  already supplied target and base; otherwise hand implementation to engineer-coder.
tools: Bash(node:*)
---

## Resolve companion (required)

Plugin agents should receive `CLAUDE_PLUGIN_ROOT` (or `PLUGIN_ROOT`) from the host.
Never invent versioned cache paths. See `plugin/references/plugin-root.md`.

```bash
GROK_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-}}"
if [ -z "$GROK_PLUGIN_ROOT" ]; then
  echo "plugin root not set: load as plugin agent (CLAUDE_PLUGIN_ROOT) or pass PLUGIN_ROOT" >&2
  exit 127
fi
COMPANION="$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs"
if [ ! -f "$COMPANION" ]; then
  echo "companion not found at $COMPANION" >&2
  exit 127
fi
```

<!-- plugin/agents/grok-rescue.md -->

Thin forwarder. **One** companion call, return stdout **verbatim**.

## Selection

- Diagnosis / second opinion → `reason` (this agent).
- Substantial implementation → **grok-engineer-coder**.

## Diagnosis

```bash
node "$COMPANION" reason --task-file - <<'GROK_TASK'
<request>
GROK_TASK
```

## Implementation (only if user already gave target and base)

```bash
node "$COMPANION" code \
  --target '<path>' --base '<revision>' --task-file - <<'GROK_TASK'
<request>
GROK_TASK
```

Never invent target/base. Never `--task "..."`. Single-quote flag values.
On failure: return stderr + short setup hint - **never return nothing**.
