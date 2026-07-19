<!-- docs/specs/2026-07-17-acp-peer-channel-design.md -->

# ACP peer channel (2.0.0) - design

Authority for Task 5.3 (plan: docs/superpowers/plans/2026-07-16-peer-agent-integration.md)
and Task 7.4 (evidence-backed ready + ACP default). Probe evidence:
docs/research/2026-07-17-acp-probe.md. **ACP is the default peer channel** for
`grok-engineer-coder`. Opt out with `GROK_DISABLE_ACP=1` (wrapper + companion)
to force one-shot `code`. Hardened-only; Claude and Codex identical.

## Status: evidence-backed peer channel (default)

Peer-stop runs the contract's `requiredValidation` and the workspace build gate
**for real** via the wrapper's `_run_recorded_command` / `_run_build_gate`
(same ordered finalize as code/worktree). `integration.ready=true` is possible
only from non-forgeable wrapper-executed evidence:

- At least one validation source with `authoritative: true` and `passed: true`
- At least one `commands[]` entry with a real `exitStatus` (never synthesized)

A forgery guard fails closed if ready is claimed without that evidence. When no
authoritative gate ran (no contract validations + no JS build gate), ready is
false with a clear `no-authoritative-validation` blocker.

`/grok:handoff` is code-mode only and refuses peer runIds
(`handoff-unavailable`); peer integration is applied by `peer stop` itself, per
the active integration mode. (The original Task 7.4 design also routed peer runs
through handoff; the peer-honesty hardening removed that - canonical:
`plugin/references/integration-modes.md`.)

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
scopes, patch, escape), **real** requiredValidation + build gate, optional
sandbox `verify_enforcement`, evidence-backed ready (or honest not-ready),
private-home destroy, terminalize run record.

## Confinement and isolation (honest parity)

- Start parity (fail closed before first prompt): sandbox profile capability,
  tool allowlist (or hard-refuse if empty), mcpServers [], web policy, cwd
  sentinel planted, original_baseline + pristine gate scripts captured, no
  .env copy.
- Stop: `verify_enforcement` against the private home/policy when possible.
  Failure adds a `sandbox-failure` blocker (forces not-ready).
  Do **not** claim "full code-mode isolation stack" when ACP did not produce
  sandbox-events telemetry - record the miss honestly.
- `pre_tool_use` deny is registered as NON-enforcement (OS sandbox enforces).
- Secret redaction: every relayed chunk, control-socket payload, and turn
  envelope passes the same `assert_no_secret_material` scan as
  `emit_envelope` (after redaction).
- Confinement label: a peer session that passed real gates with a contract
  scopes list gets `contract-scopes` (same standing as a code run); otherwise
  `worktree-final-diff-only`.

### Residual risk: per-frame progress redaction

Progress-chunk redaction is **per-frame**. A secret split across ACP
`session/update` frames may partially land in `progress.jsonl` before a full
token shape is visible to the scanner. Honest residual risk; not claimed as a
complete secret firewall.

## Manifest honesty (peer-stop)

- **DO** run contract `requiredValidation` and the wrapper build gate via
  real subprocesses. `exitStatus` is never synthesized.
- `validation.sources.*.authoritative` is true **only** for gates that
  actually ran. Vacuous bool pass (no validations configured) does not mark
  a source authoritative.
- `integration.ready` uses the same `compute_integration_ready` as code, then
  the forgery guard (`enforce_ready_evidence_guard`): ready requires
  authoritative source + commands[] evidence.
- No contract + build gate passes (JS repo) -> ready when other invariants hold.
- No contract + no gate (non-JS) -> not ready with
  `no-authoritative-validation` ("no authoritative validation ran").
- `confinement` is set **before** `write_manifest` (and its secret scan); never
  mutated on disk after the scanned write.
- Crash-path peer-stop loads `originalBaseline` from `peer.json` (captured at
  start). Never re-capture at stop (re-capture closes the escape window
  fail-open).

## Integration via the active mode

On peer-stop when ready, the companion integrates per the resolved integration
mode (setup / `--integration` / userConfig):

- **direct** (with consent) or **auto**: apply the verified patch to the target
  (`git apply --check --binary` then `git apply --binary`)
- **review** or **worktree**: leave patch + manifest; no apply

Consent gate applies for direct (same as code). `peer stop` applies the verified
patch itself per integration mode; `/grok:handoff` does NOT accept peer runs
(code-mode only).

Terminal honesty: the companion rewrites the peer-stop stdout envelope with the
true apply outcome **before** first write / store / notify (shared auto final-
envelope SSOT). Blocked apply => one `status: failure` envelope with
`response.integration.applied=false` + outcome, stored identically for
`/grok:result`, job failed, nonzero exit; success apply => one success envelope
with `applied=true`.

## Failure model

- Child death -> peer.json lifecycle "died"; peer-prompt fails closed
  `acp-failure` with reattach hint (peer-stop still finalizes artifacts from
  the worktree state).
- Timeouts per prompt (default 900s) -> session/cancel + acp-failure.
- Stale peer sessions: the stale-home reaper treats peer-active runs as
  leased while pid is alive AND younger than MAX_PEER_LEASE; else reaps.
- Every error maps to existing ERROR_CLASSES + `acp-failure`.
- `GROK_DISABLE_ACP=1` in the wrapper or companion -> `usage-error` / refuse.

## Non-goals (v1)

- No host-side ACP server; no session/load reattach (capability noted for
  v2); no multi-session mux per child; one-stdout-envelope-per-invocation
  preserved (resident: one running envelope only).

## Amendments (adversarial review 2026-07-17, REQUIRED)

1. CONTROL PLANE: wrapper-owned unix domain socket (0600, companion uid only).
2. CRASH/REAPER: peer.json records wrapper + child pid/starttime; MAX_PEER_LEASE
   separate from MAX_RUN_TIMEOUT; start baseline persisted for crash-stop.
3. START PARITY: fail closed before first prompt (sandbox capability, tools,
   mcpServers [], web, sentinel, baseline, no .env).
4. REDACTION: control-socket + turn envelopes scanned; multi-frame residual
   risk documented; child stderr dropped or redacted.
5. HANDOFF: `/grok:handoff` is code-mode only (refuses peer runIds); peer runs
   integrate via `peer stop` itself, per active mode (Task 7.4 peer-handoff
   eligibility later removed by the peer-honesty hardening).
6. pre_tool_use deny is NON-enforcement.
7. Prompts serialized: one in-flight prompt per session.
8. WRAPPER GATE: ACP default; `GROK_DISABLE_ACP=1` is the opt-out (both
   wrapper and companion). `GROK_EXPERIMENTAL_ACP` is no longer a hard gate.
9. RUN RECORD: peer-start records worktreePath/lifecycle; peer-stop
   terminalizes so cleanup can remove the worktree.
10. SINGLE STDOUT: resident peer-start emits only the running envelope.
11. FORGERY GUARD: ready=true impossible without authoritative source + real
    commands[] exitStatus (unit-tested fail-closed).

Optional (v1.1+): probe session/load to drop wrapper residency; reassemble
multi-frame chunks before scanning.
