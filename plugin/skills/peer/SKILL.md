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
`no-authoritative-validation`. Peer always runs in an **external retained
worktree** (never live-edit of the operator tree during prompts). Ready results
integrate only at **`peer stop`**, via the active mode (canonical:
`plugin/references/integration-modes.md` ACP peer section): **direct** and
**auto** both apply the verified ready patch to the target checkout (direct
applies when ready); **review** / **worktree** retain
patch + manifest for manual parent apply. Peer direct is therefore **stop-time
apply**, not one-shot code live-edit direct. `/grok:handoff` is code-mode only
and refuses peer runIds (`handoff-unavailable`); peer integration never routes
through it. Shared apply spine (exclusive lock + durable marker + pure-rename
header fail-closed): [integration-modes.md](../../references/integration-modes.md).
The companion emits **one** final stdout envelope that already includes the
apply outcome (`response.integration.applied` / `outcome`) under
rewrite-before-write/store/finalize (onStdout computes final
emitStdout/effectiveCode before first write; then write; then storeJobStdout;
then updateJob/finalize; then notify). Blocked apply is `status: failure` +
nonzero exit + failed job + identical stored `/grok:result` payload (never a
raw wrapper success for an unapplied ready peer-stop). Peer-stop is **not**
completion-notification eligible (see `NOTIFY_ELIGIBLE_MODES`). Lifecycle
honesty: durable terminal before restop success; mandatory `promptsHandled`
persist; `startToken` identity fail-closed; control frame caps (~4 MiB);
active-proc never pid-scan-unregistered on kill refusal.

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

Optional `--contract-file` is the same operator-trusted writeScopes +
`requiredValidation` contract as `code`. Present-but-blank forms
(`--contract-file` empty / `--contract-file=` / empty shell expansion) fail
closed as `implementation-contract-invalid` - never silent no-contract.

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

- Start parity (`peer_process.assert_start_parity`) fails closed before spawn
  on sandbox policy/profile, non-empty tool allowlist, no `.env` / `.env.local`
  in the worktree, and private-home posture (dir + 0700 on POSIX). It neither
  plants nor verifies the cwd sentinel. The first peer-prompt prepends the
  sentinel directive so the model creates `.grok-run-<run-id>` as its mandatory
  first action (same as code mode). Peer-stop requires sentinel proof only when
  `promptsHandled > 0` (zero-prompt sessions are exempt). The ACP child is
  spawned with the same global C6 tool / permission / web / sandbox pins the
  running envelope advertises (probe-accepted globals before `agent stdio`).
  Peer-stop reuses the **start** baseline (never re-captured at stop).
- `pre_tool_use` may appear as an initialize **capability**; the wrapper does
  **not** register a deny hook (documented NON-enforcement). OS sandbox + C6
  pins are the real layers. Peer does **not** claim full code-mode isolation
  parity when sandbox events are missing - verify_enforcement failure is
  recorded honestly. Runtime claims beyond local CLI parse+initialize probe
  evidence are not made.
- Peer lifecycle is single-flight under `run_lock` with `stopOwner` reclaim:
  concurrent stop finalizes once; field-safe peer.json RMW refuses clobbering
  stopping/stopOwner; peer-prompt refuses non-promptable lifecycles.
  **Durable-before-terminal:** restop treats a session terminal only after the
  durable terminal mark is on disk. **promptsHandled** must persist (fail closed
  if it cannot). Identity is **pid + startToken** (never reclaim a live wrapper
  on age alone). Control socket teardown is mandatory at stop; control frames
  fail closed above the frame byte cap (~4 MiB). Active-proc registry never
  pid-scan-unregisters on kill refusal (only on confirmed teardown).
- Secret redaction applies to progress chunks, turn envelopes, and control-
  socket payloads (same scan as `emit_envelope`).
- Integrate only per the chosen mode's gate (see
  `plugin/references/integration-modes.md` ACP peer section: shared dirty-guard
  apply spine with exclusive apply lock + durable marker + already-applied
  restop; review/worktree retain; auto and direct apply the verified
  ready patch at stop - not live-edit). Prefer deriving a contract with
  shell-free `requiredValidation` so ready can be evidence-backed.
