<!-- docs/dual-lens-harden.md -->

# Dual-lens harden recipe

Use two opposing Grok passes before you trust a design or a change:

1. **Attack** (`/grok:adversarial-review`) - assume the work is wrong; force
   concrete failure modes with live web grounding on by default.
2. **Assess** (`/grok:review`) - ordinary correctness / safety review of the
   same target (web off by default for determinism).
3. Optional **debate** (`/grok:debate`) when the topic is a design tradeoff
   rather than a concrete patch.

## Claude Code

```text
/grok:adversarial-review --target . --task-file - <<'GROK_TASK'
Focus on auth boundaries and data loss.
GROK_TASK

/grok:review --target . --task-file - <<'GROK_TASK'
Re-read the same surface after the adversarial pass. Confirm or refute each
high/critical attack. Prefer residual risks over re-stating praise.
GROK_TASK
```

## Codex / CLI (prefer skill runners)

```bash
# After Skill tool load (preferred):
SKILL_BASE='…/skills/dual-lens'   # or adversarial-review / review skill dirs
node "$SKILL_BASE/run.mjs" adversarial-review \
  --target . --task-file - <<'GROK_TASK'
Focus on auth boundaries and data loss.
GROK_TASK

node "$SKILL_BASE/run.mjs" review \
  --target . --task-file - <<'GROK_TASK'
Confirm or refute high/critical attacks from the prior pass.
GROK_TASK

# Or known install (optional):
# node "${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}/scripts/grok-companion.mjs" …
```

## How to read results

- Prefer severity-ranked findings from the adversarial pass.
- Treat `warnings` containing `grounding-requested-no-sources` as "this run
  claimed web but produced no sources" - re-run with `--web` or fix auth/network.
- Pretty print: `/grok:result --pretty` (or companion `result --pretty`) shows a
  Sources section when `citations` are present.
- Do not merge the two passes into one prompt; the point is two lenses.

## Skill shortcut

`/grok:dual-lens` walks the agent through this recipe without inventing a third
wrapper mode.
