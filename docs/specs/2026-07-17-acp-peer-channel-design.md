<!-- docs/specs/2026-07-17-acp-peer-channel-design.md -->

# ACP peer channel (2.0.0 experimental) - design

Authority for Task 5.3 (plan: docs/superpowers/plans/2026-07-16-peer-agent-integration.md).
Probe evidence: docs/research/2026-07-17-acp-probe.md. Gated behind
GROK_EXPERIMENTAL_ACP=1; hardened-only; Claude and Codex identical (companion
internal).

## Architecture

One long-lived peer SESSION = one run id + one private home + one external
worktree + one `grok agent stdio` child (spawned in that env, cwd worktree,
private leader socket). The wrapper process does NOT stay resident: peer-start
spawns the child detached (own group, same registry as live runs),
peer-prompt/peer-stop reattach over the child's stdio via a tiny
wrapper-owned unix socket bridge... REJECTED for v1: stdio cannot be
reattached across processes. v1 DESIGN: the child lives as long as the
peer-start WRAPPER process, which stays resident in the background (companion
spawns it with run_in_background semantics); prompts are delivered through a
run-dir FIFO protocol:

- `runs/<id>/peer/inbox/` - prompt files (NNNN-prompt.md, atomic rename in)
- `runs/<id>/peer/outbox/` - per-prompt result envelopes (NNNN-result.json)
- `runs/<id>/peer.json` - {pid, sessionId, lifecycle, startedAtUtc}
- progress.jsonl - session/update chunks relayed through the EXISTING
  redaction pipeline (groklib.redaction) as progress events

peer-prompt = companion writes an inbox file and tails the outbox (bounded
wait); peer-stop = inbox sentinel `stop`, wrapper finalizes: session/cancel,
child teardown, THEN the standard code finalize path (scopes from an optional
contract, forensic patch, build gate, manifest) so integration STILL goes
through /grok:handoff dual-condition ready. No ACP bypass of the patch
protocol, ever.

## Confinement

- Same policy stack as code mode: private home, sandbox profile TOML, worktree
  cwd, tool allowlist via session config where ACP exposes it.
- ADDITIONAL: register the x.ai pre_tool_use blocking hook and DENY any tool
  call whose declared paths resolve outside the worktree (defense in depth in
  front of Seatbelt; deny is advisory-strength only - the OS sandbox remains
  the enforcement layer; document honestly).
- Secret redaction: every relayed chunk passes the existing pattern scan
  before progress.jsonl; envelopes unchanged.

## Failure model

- Child death -> peer.json lifecycle "died"; peer-prompt fails closed
  `acp-failure` with reattach hint (peer-stop still finalizes artifacts from
  the worktree state).
- Timeouts per prompt (default 900s) -> session/cancel + acp-failure.
- Stale peer sessions: the stale-home reaper treats peer-active runs as
  leased while pid is alive AND younger than MAX_RUN_TIMEOUT; else reaps.
- Every error maps to existing ERROR_CLASSES + new `acp-failure`.

## Non-goals (v1)

- No host-side ACP server; no session/load reattach (capability noted for
  v2); no multi-session mux per child; no auto-apply; one-envelope-per-
  invocation preserved for every companion command.
