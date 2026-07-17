---
name: "peer"
description: "Experimental ACP peer channel (start/prompt/stop) behind GROK_EXPERIMENTAL_ACP=1"
argument-hint: "start --target <path> --base <rev> [--contract-file <path>] [--model] | prompt --run-id <id> (--task|--task-file) | stop --run-id <id>"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Experimental gate

This skill is **experimental**. The companion refuses every `peer` mode unless:

```bash
export GROK_EXPERIMENTAL_ACP=1
```

Design authority: `docs/specs/2026-07-17-acp-peer-channel-design.md` (Amendments
supersede the draft). Hardened-only (no direct run-mode).

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
`grok agent stdio` child. Emits one envelope with `status: "running"` and
`response.peer {sessionId, socketPath}`. The wrapper stays resident serving a
wrapper-owned unix control socket (0600).

```bash
export GROK_EXPERIMENTAL_ACP=1
node "$SKILL_BASE/run.mjs" peer start --target '<path>' --base '<rev>' [--contract-file '<path>'] [--model '<id>']
```

### peer prompt

Sends one serialized prompt to a running session (one in-flight at a time).
Relays redacted `session/update` chunks to `progress.jsonl`. Emits one
redacted turn envelope.

```bash
export GROK_EXPERIMENTAL_ACP=1
node "$SKILL_BASE/run.mjs" peer prompt --run-id '<id>' --task-file - <<'GROK_TASK'
<task text>
GROK_TASK
```

### peer stop

Cancels the ACP session, tears down the child and resident wrapper, runs the
existing code handoff finalize path, labels confinement
`worktree-final-diff-only` unless a contract with scopes was supplied, and
destroys the private home.

```bash
export GROK_EXPERIMENTAL_ACP=1
node "$SKILL_BASE/run.mjs" peer stop --run-id '<id>'
```

## Safety notes

- Start parity matches code mode (sandbox, tools, mcpServers [], web policy,
  cwd sentinel, baseline, no .env) and fails closed before spawn.
- `pre_tool_use` deny is registered but documented as **NON-enforcement**; the
  OS sandbox is the enforcement layer.
- Secret redaction applies to progress chunks and turn envelopes.
- No auto-apply; integrate only via `/grok:handoff` dual-condition ready.
