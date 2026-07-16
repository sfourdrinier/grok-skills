---
name: "status"
description: "Read a Grok run by id: finished envelope, or running progress if still in progress (read-only)"
argument-hint: "[--run-id <run-id>]"
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

<!-- plugin/skills/status.md -->

Read a Grok run by id through the hardened wrapper and relay its result
envelope. Projection (read-only; never writes the target run dir):

- In-flight (`created` / `running` / `finalizing`) → top-level `"running"`, exit 0
- Completed → `"success"`, exit 0, with `response.storedEnvelope` when present
- Failed / canceled / derived interrupted → `"failure"`, exit **1**, still a
  well-formed status envelope (exit 1 means the *target* failed or was
  interrupted, not that the status command malfunctioned)

`response.target` includes `lifecycle`, `lifecycleSource` (`record` |
`envelope` | `derived`), `elapsedMs`, process liveness, and progress summaries.
Always relay the JSON envelope to the user regardless of exit status.

Raw slash-command arguments:
`$ARGUMENTS`

Required wrapper flags (copy exactly, substitute only placeholder values):
- `--run-id <run-id>` is required. If the user did not supply one, ask them for
  the run id (it is printed in every run's envelope as `runId`) BEFORE running
  anything.
- Preserve the user's arguments exactly. Do not strip, add, or reorder flags.
  Do not invent a flag that is not in the argument-hint.

Run it as one Bash call and relay the result. SINGLE-QUOTE the run id so it
reaches the companion as one literal argv element; NEVER embed the raw argument
inside a position the shell would evaluate. An unquoted OR double-quoted value
containing `$(...)`/backticks is command-substituted locally BEFORE the wrapper
ever validates it. Single quotes pass the bytes verbatim; the wrapper then
rejects any run id that is not the strict `YYYYMMDDThhmmssZ-xxxxxx` run-id shape:
```bash
node "$SKILL_BASE/run.mjs" status --run-id '<run-id from $ARGUMENTS>'
```
- Return the command stdout envelope to the user VERBATIM. Do not paraphrase,
  summarize, reformat, or add commentary before or after it. Preserve the exit
  status: exit 1 with a parseable status envelope is a successful inspection of
  a failed/interrupted target — still show the envelope.

If the companion prints an actionable "could not locate the Grok wrapper"
message instead of an envelope, tell the user to run `/grok:setup`.

Without `--run-id`, the companion prints the local **jobs table** for this workspace
(recent companion-tracked runs). With `--run-id`, it returns the wrapper status envelope.

Also: `/grok:jobs`, `/grok:result`, `/grok:cancel`.
