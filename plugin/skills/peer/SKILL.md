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
integration mode (canonical: `plugin/references/integration-modes.md` -
direct: live tree; auto: apply-on-ready; review: patch + manifest only) -
applied by `peer stop` itself, at stop time. `/grok:handoff` is code-mode only
and refuses peer runIds (`handoff-unavailable`); peer integration never routes
through it (see `integration-modes.md`). The companion emits **one** final
stdout envelope that already includes the apply outcome
(`response.integration.applied` / `outcome`) under rewrite-before-write/store/finalize
(onStdout computes final emitStdout/effectiveCode before first write; then write;
then storeJobStdout; then updateJob/finalize; then notify). Blocked apply is
`status: failure` + nonzero exit + failed job + identical stored `/grok:result`
payload (never a raw wrapper success for an unapplied ready peer-stop). Peer-stop
is **not** completion-notification eligible (see `NOTIFY_ELIGIBLE_MODES`).

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

- Start parity requires/verifies the cwd sentinel **contract** (sandbox
  capability, tool allowlist, private home, baseline, no `.env`) before spawn
  and fails closed if unmet, but does **not** plant the sentinel. The model
  creates `.grok-run-<run-id>` as its mandatory first action on the first
  peer-prompt (same as code mode), so stop-time sentinel proof is genuine.
  The ACP child is spawned with the same global C6 tool / permission / web /
  sandbox pins the running envelope advertises (probe-accepted globals before
  `agent stdio`). Peer-stop reuses the **start** baseline (never re-captured
  at stop).
- `pre_tool_use` may appear as an initialize **capability**; the wrapper does
  **not** register a deny hook (documented NON-enforcement). OS sandbox + C6
  pins are the real layers. Peer does **not** claim full code-mode isolation
  parity when sandbox events are missing - verify_enforcement failure is
  recorded honestly. Runtime claims beyond local CLI parse+initialize probe
  evidence are not made.
- Peer lifecycle is single-flight under `run_lock` with `stopOwner` reclaim:
  concurrent stop finalizes once; field-safe peer.json RMW refuses clobbering
  stopping/stopOwner; peer-prompt refuses non-promptable lifecycles.
- Secret redaction applies to progress chunks, turn envelopes, and control-
  socket payloads (same scan as `emit_envelope`).
- Integrate only per the chosen mode's gate (see
  `plugin/references/integration-modes.md`: shared dirty-guard apply spine;
  review never auto-applies; auto may after revalidation + patch integrity;
  direct lands live). Prefer deriving a contract with shell-free
  `requiredValidation` so ready can be evidence-backed.
