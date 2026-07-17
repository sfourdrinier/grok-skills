---
name: "peer"
description: "ACP multi-turn peer channel (start/prompt/stop). Default peer path; opt out with GROK_DISABLE_ACP=1"
argument-hint: "start --target <path> --base <rev> [--contract-file <path>] [--model] | prompt --run-id <id> (--task|--task-file) | stop --run-id <id>"
allowed-tools: "Bash(node:*), Bash(git:*), AskUserQuestion"
---

## Default peer channel

This skill drives the live multi-turn ACP peer (`peer start|prompt|stop`). It is
the **default** implementation path for `grok-engineer-coder`. One-shot `code`
is the fallback when ACP is disabled or unavailable.

Opt out of ACP (force one-shot only):

```bash
export GROK_DISABLE_ACP=1
```

Design authority: `docs/specs/2026-07-17-acp-peer-channel-design.md`. Hardened-
only (no direct run-mode).

## Evidence-backed ready

`peer stop` runs contract `requiredValidation` and the workspace build gate
**for real** (wrapper-executed commands). `integration.ready=true` only when:

1. An authoritative validation source passed, and
2. `commands[]` carries a real `exitStatus` (never forged)

No contract + no build gate -> honest not-ready with
`no-authoritative-validation`. Ready peer results integrate via the active
integration mode (auto/direct apply the verified patch; review leaves patch +
manifest). `/grok:handoff --run-id` also works on a peer run (mode peer-stop).

## How to run (transparent)

1. Take the absolute **Base directory for this skill** from the Skill tool
   (the folder that contains this skill's `SKILL.md` and `run.mjs`).
2. Set `SKILL_BASE` to that path. Do **not** invent versioned cache paths.
3. Always invoke the companion **only** through this skill's runner:

```bash
SKILL_BASE='<Base directory for this skill - absolute path from Skill tool>'
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
node "$SKILL_BASE/run.mjs" peer start --target '<path>' --base '<rev>' [--contract-file '<path>'] [--model '<id>']
```

### peer prompt

Sends one serialized prompt to a running session (one in-flight at a time).
Relays redacted `session/update` chunks to `progress.jsonl` (per-frame
redaction; a secret split across ACP frames may partially land - residual
risk). Emits one redacted turn envelope. Control-socket payloads are
secret-scanned before leave.

```bash
node "$SKILL_BASE/run.mjs" peer prompt --run-id '<id>' --task-file - <<'GROK_TASK'
<task text>
GROK_TASK
```

### peer stop

Cancels the ACP session, tears down the child and resident wrapper, runs
forensic finalize (sentinel / scopes / patch / escape), **executes**
requiredValidation + build gate, labels confinement (`contract-scopes` when a
contract with scopes was supplied, else `worktree-final-diff-only`), attempts
sandbox `verify_enforcement` against the private home (failure is a blocker),
writes the evidence-backed handoff manifest, terminalizes the run, and
destroys the private home. The terminal envelope is emitted by **this**
invocation. When ready, the companion integrates per the active mode.

```bash
node "$SKILL_BASE/run.mjs" peer stop --run-id '<id>' [--integration auto|review|direct|worktree]
```

After stop, remove the retained worktree when finished:

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
- Never auto-apply beyond the chosen integration mode's gate. Prefer deriving a
  contract with shell-free `requiredValidation` so ready can be evidence-backed.
