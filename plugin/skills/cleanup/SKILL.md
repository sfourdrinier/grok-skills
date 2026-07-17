---
name: "cleanup"
description: "Report (dry-run) or remove a Grok run's owned session state and worktree by run id"
argument-hint: "--run-id <run-id> [--confirm]"
allowed-tools: "Bash(node:*)"
---

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool
   (the folder that contains this skill's `SKILL.md` and `run.mjs`).
2. Set `SKILL_BASE` to that path. Do **not** invent versioned cache paths.
3. Always invoke the companion **only** through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
node "$SKILL_BASE/run.mjs" <mode> [args...]
```

`run.mjs` finds the plugin install from its own location and runs
`scripts/grok-companion.mjs`. No `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` required.

If the host already exported `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT`, you may call
`node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs"` instead; prefer
`"$SKILL_BASE/run.mjs"` whenever the Skill tool loaded this skill.

Return companion **stdout verbatim**. Never put free-text in `--task "..."`;
use `--task-file -` with a single-quoted heredoc.

<!-- plugin/skills/cleanup.md -->

Report (dry-run) or remove a Grok run's owned session state and worktree by run
id through the hardened wrapper and relay its result envelope. It prints exactly
one JSON result envelope.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--run-id <run-id>` is required. If the user did not supply one, ask for it
  BEFORE running anything.
- `--confirm` is optional and passed through only when the user supplied it.
  - Without `--confirm` it is a DRY RUN: it reports the owned session state and
    (for `code`/`verify`) the worktree and branch it WOULD remove. Nothing is
    deleted.
  - With `--confirm` it actually removes them. Removal is gated on OWNERSHIP,
    not cleanliness: the wrapper refuses (fail closed) when the sibling owner
    marker is missing, unmarked/foreign, or its run id does not match the
    requested `--run-id`, and when the worktree directory name does not match
    the requested `--run-id`. When both DO match the requested run, the worktree
    is removed whether it is clean OR dirty (`code` mode intentionally leaves its
    worktree dirty), so a dirty owner-marked worktree owned by the requested run
    is removed, never refused.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.

Run it as one Bash call and relay the result.
- Injection safety (canonical rationale: `plugin/references/argv-safety.md`):
  Wrap every substituted flag VALUE in single quotes (`--run-id '<run-id>'`).
  Bare flags (`--web`) carry no value to quote. The wrapper then rejects any run
  id that is not the strict `YYYYMMDDThhmmssZ-xxxxxx` run-id shape and binds the
  destructive removal to the requested run id:
```bash
node "$SKILL_BASE/run.mjs" cleanup --run-id '<run-id from $ARGUMENTS>' [--confirm]
```
- Return the command stdout envelope to the user VERBATIM. Do not paraphrase,
  summarize, reformat, or add commentary before or after it, and preserve the
  exit status.

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.
