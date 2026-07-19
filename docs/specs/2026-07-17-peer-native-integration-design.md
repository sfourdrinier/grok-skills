<!-- docs/specs/2026-07-17-peer-native-integration-design.md -->

# Phase 7: peer-native integration re-architecture (remediation)

The 2.0 build made patch-apply the ONLY integration path and flagged ACP off.
Both betray the goal (Grok as a peer, peer to Opus/Sonnet). This inverts the
defaults: Grok edits your tree directly by default, and ACP is the default
peer channel. The worktree+handoff machinery becomes opt-in, not gone.

## Integration modes (wrapper + companion + setup/userConfig default)

`--integration <mode>` on code/implement/peer; workspace default via setup.

- **direct (NEW DEFAULT) = hardened-direct**: Grok edits the OPERATOR's real
  working tree. Retained safety: private auth home (creds isolated), OS sandbox
  write-confined to the REPO ROOT (+ private tmp), secret redaction on output.
  DROPPED vs worktree: no isolation/rollback, no pre-apply dual-condition gate
  (edits are live), no forensic patch. This is the trusted-input posture,
  documented loudly. Equivalent trust level to an Opus/Sonnet subagent editing
  your tree - which is the goal.
  - Sub-option `--integration direct --raw` (or run-mode direct): the existing
    installed-CLI path, zero wrapper safety, fastest.
- **auto (opt-in)**: current worktree flow with EVERY safety check, PLUS on
  dual-condition READY the companion auto-applies the patch to your tree
  (git apply --binary). Native feel + full verification before anything lands.
  Not-ready -> nothing applied, blockers surfaced.
- **review (opt-in, = current 2.0 behavior)**: worktree + handoff + patch,
  NEVER auto-apply. For untrusted changes / review-first.

## ACP as the default peer channel

- Un-gimp peer-stop: run the contract's requiredValidation + build gate FOR
  REAL (not faked, not skipped). When they pass, the peer result is
  integration-ready and integrates via the active mode (direct: already live;
  auto: apply; review: patch). Reverses the "never ready" honesty-patch by
  making validation genuine.
- grok-engineer-coder DEFAULTS to the live multi-turn ACP peer; one-shot code
  is the opt-out fallback.
- ACP is default on; opt out of the peer channel with `GROK_DISABLE_ACP=1`
  (one-shot `code` fallback). `GROK_EXPERIMENTAL_ACP` is no longer a hard gate
  (legacy opt-in ignored).

## Sandbox note (design-review target)

Hardened-direct needs the sandbox profile's writable root pointed at the
operator repo root instead of a worktree. Confirm sandbox.py supports an
arbitrary write root and that verify_enforcement still holds. If a live repo
has a dev server / other writers, direct edits race with them - documented,
not guarded (same trusted-input honesty as review-mode FS-drift notes).

## Docs + DRY

- One canonical integration-modes reference; every "never auto-apply" /
  "parent apply is manual" statement becomes mode-aware and REFERENCES it
  (no copy-paste). README + SECURITY loudly state direct-as-default.
- Manifest single-source generator (tools/gen-manifests.mjs, CI + pre-commit,
  drift test as guard); finish fixture consolidation onto helpers/fake-wrapper.

## Non-goals

- Do not remove the worktree/handoff machinery (it is auto/review).
- Do not weaken auto/review guarantees.
- Version stays 2.0.0 (unreleased) - this is part of the same release.
