<!-- docs/specs/2026-07-17-acp-peer-channel-design.md -->

# ACP peer channel (2.0.0 experimental preview) - design

Authority for Task 5.3 (plan: docs/superpowers/plans/2026-07-16-peer-agent-integration.md).
Probe evidence: docs/research/2026-07-17-acp-probe.md. Gated behind
`GROK_EXPERIMENTAL_ACP=1` in **both** the companion and the wrapper (fail closed
with `usage-error` when unset). Hardened-only; Claude and Codex identical.

## Status: honest experimental preview

Peer-preview produces a retained worktree + an honest handoff **manifest that is
never integration-ready** (`integration.ready` is always false with a
`handoff-unavailable` blocker). It is **not** eligible for `/grok:handoff`
(handoff requires a code-mode terminal envelope; peer runs are mode
`peer-start`). Integration is **manual** from the retained worktree after
operator review. Do not claim dual-condition handoff or code-parity readiness.

## Architecture

One long-lived peer SESSION = one run id + one private home + one external
worktree + one `grok agent stdio` child (spawned in that env, cwd worktree,
private leader socket). v1 DESIGN: the child lives as long as the peer-start
WRAPPER process, which stays resident (companion spawns it with
run_in_background semantics). Control plane is a wrapper-owned unix domain
socket (0600, companion uid only) - not a FIFO.

- `runs/<id>/peer.json` - wrapper+child pid/starttime, worktree paths, start
  `originalBaseline`, lease, lifecycle
- `runs/<id>/run.json` - lifecycle + `worktreePath` / `worktreeBranch` /
  `baseRevision` (same ownership fields as code mode so `cleanup --run-id`
  can rebuild and remove the external worktree)
- `progress.jsonl` - session/update chunks relayed through the existing
  redaction pipeline (per-frame; see residual risk below)

peer-start emits **exactly one** stdout envelope (`status: "running"`) for the
resident process lifetime. peer-prompt / peer-stop are separate invocations;
peer-stop emits the terminal outcome. The resident does not print a second
stdout envelope (terminal is on the control socket + durable run dir).

peer-stop finalizes: session/cancel, child teardown, forensic path (sentinel,
scopes, patch, escape), optional sandbox `verify_enforcement`, honest
not-ready manifest, private-home destroy, terminalize run record.

## Confinement and isolation (honest parity)

- Start parity (fail closed before first prompt): sandbox profile capability,
  tool allowlist (or hard-refuse if empty), mcpServers [], web policy, cwd
  sentinel planted, original_baseline + pristine gate scripts captured, no
  .env copy.
- Stop: `verify_enforcement` against the private home/policy when possible.
  Failure adds a `sandbox-failure` blocker (integration.ready already false).
  Do **not** claim "full code-mode isolation stack" when ACP did not produce
  sandbox-events telemetry - record the miss honestly.
- `pre_tool_use` deny is registered as NON-enforcement (OS sandbox enforces).
- Secret redaction: every relayed chunk, control-socket payload, and turn
  envelope passes the same `assert_no_secret_material` scan as
  `emit_envelope` (after redaction).

### Residual risk: per-frame progress redaction

Progress-chunk redaction is **per-frame**. A secret split across ACP
`session/update` frames may partially land in `progress.jsonl` before a full
token shape is visible to the scanner. Honest residual risk for the preview;
not claimed as a complete secret firewall.

## Manifest honesty (peer-stop)

- Do **not** run contract `requiredValidation` or the wrapper build gate.
- Do **not** forge `exit_status` (or any other validation evidence).
- `validation.sources.wrapperBuildGate` and
  `validation.sources.contractRequiredValidation` are always
  `authoritative: false` with reason `peer-preview: not executed`.
- `integration.ready` is always false with blocker
  `{kind: "handoff-unavailable", message: "peer-preview runs are not
  integration-ready; apply from the worktree manually after your own review"}`.
- `confinement` is set **before** `write_manifest` (and its secret scan); never
  mutated on disk after the scanned write.
- Crash-path peer-stop loads `originalBaseline` from `peer.json` (captured at
  start). Never re-capture at stop (re-capture closes the escape window
  fail-open).

## Failure model

- Child death -> peer.json lifecycle "died"; peer-prompt fails closed
  `acp-failure` with reattach hint (peer-stop still finalizes artifacts from
  the worktree state).
- Timeouts per prompt (default 900s) -> session/cancel + acp-failure.
- Stale peer sessions: the stale-home reaper treats peer-active runs as
  leased while pid is alive AND younger than MAX_PEER_LEASE; else reaps.
- Every error maps to existing ERROR_CLASSES + `acp-failure`.
- Experimental flag missing in the wrapper -> `usage-error`.

## Non-goals (v1)

- No host-side ACP server; no session/load reattach (capability noted for
  v2); no multi-session mux per child; no auto-apply; no dual-condition
  handoff path for peer; one-stdout-envelope-per-invocation preserved
  (resident: one running envelope only).

## Amendments (adversarial review 2026-07-17, REQUIRED)

1. CONTROL PLANE: wrapper-owned unix domain socket (0600, companion uid only).
2. CRASH/REAPER: peer.json records wrapper + child pid/starttime; MAX_PEER_LEASE
   separate from MAX_RUN_TIMEOUT; start baseline persisted for crash-stop.
3. START PARITY: fail closed before first prompt (sandbox capability, tools,
   mcpServers [], web, sentinel, baseline, no .env).
4. REDACTION: control-socket + turn envelopes scanned; multi-frame residual
   risk documented; child stderr dropped or redacted.
5. HANDOFF HONESTY: peer-preview is never integration-ready; not eligible for
   `/grok:handoff`; manual apply from retained worktree only.
6. pre_tool_use deny is NON-enforcement.
7. Prompts serialized: one in-flight prompt per session.
8. WRAPPER GATE: `GROK_EXPERIMENTAL_ACP=1` enforced in the wrapper, not
   companion-only.
9. RUN RECORD: peer-start records worktreePath/lifecycle; peer-stop
   terminalizes so cleanup can remove the worktree.
10. SINGLE STDOUT: resident peer-start emits only the running envelope.

Optional (v1.1+): probe session/load to drop wrapper residency; reassemble
multi-frame chunks before scanning.
