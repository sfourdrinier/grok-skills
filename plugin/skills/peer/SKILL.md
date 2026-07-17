---
name: "peer"
description: "Experimental ACP peer-preview channel (start/prompt/stop) behind GROK_EXPERIMENTAL_ACP=1"
argument-hint: "start --target <path> --base <rev> [--contract-file <path>] [--model] | prompt --run-id <id> (--task|--task-file) | stop --run-id <id>"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Experimental gate

This skill is an **experimental preview**. It is **not** integration-ready and
is **not** eligible for `/grok:handoff`. Both the companion **and** the wrapper
refuse every `peer` mode unless:

```bash
export GROK_EXPERIMENTAL_ACP=1
```

Design authority: `docs/specs/2026-07-17-acp-peer-channel-design.md` (Amendments
supersede the draft). Hardened-only (no direct run-mode).

## Honest preview (read this)

`peer stop` produces a retained **worktree** plus an honest handoff **manifest**
that is always `integration.ready: false` with a `handoff-unavailable` blocker.
Validation sources (`wrapperBuildGate`, `contractRequiredValidation`) are marked
`authoritative: false` with reason `peer-preview: not executed` - the preview
does **not** run contract `requiredValidation` or the wrapper build gate, and
never forges `exit_status`.

**Integration is manual:** review the retained worktree yourself, then apply
changes from that worktree by hand. Do **not** call `/grok:handoff` for peer
runs (`/grok:handoff` requires a code-mode terminal envelope; peer mode is
`peer-start` and never produces one).

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool
   (the folder that contains this skill's `SKILL.md` and `run.mjs`).
2. Set `SKILL_BASE` to that path. Do **not** invent versioned cache paths.
3. Always invoke the companion **only** through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
export GROK_EXPERIMENTAL_ACP=1
export GROK_COMPANION_EXECUTION_CONTEXT=foreground
node "$SKILL_BASE/run.mjs" peer <start|prompt|stop> [args...]
```

Return companion **stdout verbatim**. Never put free-text in `--task "..."`;
use `--task-file -` with a single-quoted heredoc.

## Modes

### peer start

Creates a long-lived peer session: private home + external worktree +
`grok agent stdio` child. Emits **exactly one** stdout envelope with
`status: "running"` and `response.peer {sessionId, socketPath}`. The wrapper
stays resident serving a wrapper-owned unix control socket (0600). Records
`worktreePath` / lifecycle on `run.json` so later `cleanup --run-id` can remove
the external worktree.

```bash
export GROK_EXPERIMENTAL_ACP=1
node "$SKILL_BASE/run.mjs" peer start --target '<path>' --base '<rev>' [--contract-file '<path>'] [--model '<id>']
```

### peer prompt

Sends one serialized prompt to a running session (one in-flight at a time).
Relays redacted `session/update` chunks to `progress.jsonl` (per-frame
redaction; a secret split across ACP frames may partially land - residual
preview risk). Emits one redacted turn envelope. Control-socket payloads are
secret-scanned before leave.

```bash
export GROK_EXPERIMENTAL_ACP=1
node "$SKILL_BASE/run.mjs" peer prompt --run-id '<id>' --task-file - <<'GROK_TASK'
<task text>
GROK_TASK
```

### peer stop

Cancels the ACP session, tears down the child and resident wrapper, runs
forensic finalize (sentinel / scopes / patch / escape), labels confinement
`worktree-final-diff-only` unless a contract with scopes was supplied, attempts
sandbox `verify_enforcement` against the private home (failure is a blocker,
never silent success), writes the **not-ready** preview manifest, terminalizes
the run, and destroys the private home. The terminal envelope is emitted by
**this** invocation (not a second stdout write from the resident peer-start).

```bash
export GROK_EXPERIMENTAL_ACP=1
node "$SKILL_BASE/run.mjs" peer stop --run-id '<id>'
```

After stop, remove the retained worktree when finished reviewing:

```bash
node "$SKILL_BASE/run.mjs" cleanup --run-id '<id>' --confirm
```

## Safety notes

- Start parity plants cwd sentinel, private home, tool allowlist, and baseline
  before spawn (fail closed if unmet). Peer-stop reuses the **start** baseline
  (never re-captured at stop).
- `pre_tool_use` deny is registered but documented as **NON-enforcement**; the
  OS sandbox is the enforcement layer. Peer does **not** claim full code-mode
  isolation parity when sandbox events are missing - verify_enforcement failure
  is recorded honestly.
- Secret redaction applies to progress chunks, turn envelopes, and control-
  socket payloads (same scan as `emit_envelope`).
- No auto-apply; **not** eligible for `/grok:handoff`. Manual apply only from
  the retained worktree after operator review.
