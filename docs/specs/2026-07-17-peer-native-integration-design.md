<!-- docs/specs/2026-07-17-peer-native-integration-design.md -->

# Phase 7: peer-native integration re-architecture (remediation)

The 2.0 build made patch-apply the ONLY integration path and flagged ACP off.
Both betray the goal (Grok as a peer, peer to Opus/Sonnet). This inverts the
defaults: one-shot code defaults to consented live-tree edit landing, and ACP is
the default peer channel. The worktree+handoff machinery becomes opt-in for
one-shot code, not gone.

## Integration modes (wrapper + companion + setup/userConfig default)

`--integration <mode>` on code/implement/peer; workspace default via setup.

The bullets below are the **one-shot `code`** landing story (and product default
names). ACP peer isolation/landing is **not** the same for `direct` - see
[Runtime truth](#runtime-truth-docs-follow-code) and the canonical matrix.

- **direct (NEW DEFAULT) = hardened-direct (one-shot code)**: Grok edits the
  OPERATOR's real working tree. Retained safety: private auth home (creds
  isolated), OS sandbox write-confined to the REPO ROOT (+ private tmp), secret
  redaction on output. DROPPED vs worktree: no isolation/rollback, no pre-apply
  dual-condition gate (edits are live), no forensic patch. This is the
  trusted-input posture for one-shot code, documented loudly. Equivalent trust
  level to an Opus/Sonnet subagent editing your tree - which is the goal for
  that path.
  - **Orthogonal security axis (not an integration sub-flag):** `runMode`
    hardened (default) vs `runMode` direct. Use companion
    `setup --run-mode direct` / `GROK_SKILLS_MODE=direct` for the installed-CLI
    / normal-home path. There is **no** `--integration direct --raw` flag;
    do not invent one. runMode and integration both use the word "direct" -
    disambiguate via [integration-modes.md](../../plugin/references/integration-modes.md).
- **auto (opt-in)**: current worktree flow with EVERY safety check, PLUS on
  dual-condition READY the companion auto-applies the patch to your tree
  (git apply --binary). Native feel + full verification before anything lands.
  Not-ready -> nothing applied, blockers surfaced.
- **review (opt-in, = current 2.0 behavior)**: worktree + handoff + patch,
  NEVER auto-apply. For untrusted changes / review-first.

## ACP as the default peer channel

- Un-gimp peer-stop: run the contract's requiredValidation + build gate FOR
  REAL (not faked, not skipped). When they pass, the peer result is
  integration-ready and integrates at **peer-stop** via the active mode
  (direct + auto: apply verified ready patch; review: retain patch). Reverses
  the "never ready" honesty-patch by making validation genuine.
- grok-engineer-coder DEFAULTS to the live multi-turn ACP peer; one-shot code
  is the opt-out fallback.
- ACP is default on; opt out of the peer channel with `GROK_DISABLE_ACP=1`
  (one-shot `code` fallback). `GROK_EXPERIMENTAL_ACP` is no longer a hard gate
  (legacy opt-in ignored).

## Runtime truth (docs-follow-code)

Shipped runtime (do not re-derive from early design prose above):

1. **ACP peer always** creates an external retained worktree at `peer start`
   (private home + sandbox-to-worktree). Prompt-time edits never live-edit the
   operator checkout.
2. At **peer-stop**, `integration=direct` and `integration=auto` both apply the
   verified ready patch to the target checkout via the shared apply spine;
   `direct` additionally requires per-repo direct consent. `review` /
   `worktree` retain patch + manifest for manual parent apply.
3. Therefore **peer direct != one-shot code direct**. Code direct is live-edit
   hardened-direct. Peer direct is stop-time apply after always-isolated work.
4. **runMode** stays orthogonal (peer is hardened-only). Canonical product
   matrix: [plugin/references/integration-modes.md](../../plugin/references/integration-modes.md)
   (ACP peer section).

## Sandbox note (design-review target)

One-shot hardened-direct needs the sandbox profile's writable root pointed at
the operator repo root instead of a worktree. Confirm sandbox.py supports an
arbitrary write root and that verify_enforcement still holds. If a live repo
has a dev server / other writers, direct edits race with them - documented,
not guarded (same trusted-input honesty as review-mode FS-drift notes). ACP
peer keeps sandbox write root on the external worktree for the session.

## Docs + DRY

- One canonical integration-modes reference; every "never auto-apply" /
  "parent apply is manual" / "direct lands live" statement is mode-aware **and**
  channel-aware (code vs peer) and REFERENCES it (no copy-paste).
- README + SECURITY state code direct-as-default and peer stop-time apply
  honestly.
- Manifest single-source generator (tools/gen-manifests.mjs, CI + pre-commit,
  drift test as guard); finish fixture consolidation onto helpers/fake-wrapper.

## Non-goals

- Do not remove the worktree/handoff machinery (it is auto/review for code, and
  always-on isolation for ACP peer).
- Do not weaken auto/review guarantees.
- Do not pretend peer direct is live-edit of the operator tree.
- Version stays 2.0.0 (unreleased) - this is part of the same release.
