<!-- docs/superpowers/plans/2026-07-17-phase7-peer-native.md -->

# Phase 7: peer-native integration - remediation plan

Design: docs/specs/2026-07-17-peer-native-integration-design.md (adversarially
reviewed 2026-07-17; guards below are the review's non-negotiables).
Same cadence as Phases 0-6: TDD, Grok-delegated where it fits, tools/verify.sh
gate every integration, adversarial review per task cluster, live-smoke ledger.
Version stays 2.0.0 (unreleased). DRY is a REQUIREMENT here, not a compromise.

## Decisions (locked with the operator)
- direct = DEFAULT integration mode (Grok edits the real tree, like Opus).
- auto (worktree + apply-on-verified-ready) and review (never-apply, = current
  2.0) are opt-in.
- ACP = DEFAULT peer channel; one-shot code = fallback.
- Review guard: direct-default is NOT silent - one-time setup/first-run consent.

---

### Task 7.1 - Integration-mode framework + hardened-direct (wrapper)
Files: groklib/modes/code.py, new groklib/modes/direct.py, groklib/sandbox.py,
groklib/modes/_shared.py/_worktree.py, grok_agent.py (argparse:
--integration direct|auto|review, --raw), tests.
- hardened-direct: sandbox writable_root = repo root + private tmp; private auth
  home retained; secret redaction retained.
- DENY-LIST (sandbox grant AND post-run path scan, defense in depth): .git/**,
  .env, .env.*, **/*.pem, **/credentials*, **/id_rsa*, git hooks. A write to any
  -> hard fail (new deny-list-violation error class).
- verify_enforcement HARD-fail for direct (no soft "blocker but success").
- direct finalize checks REPLACE worktree theater: realpath-under-repo on every
  changed path, contract scopes, deny-glob scan, D1(b) gate-script integrity,
  recorded validation. Marker/sentinel lives in the run-state dir, never the repo.
- dirty-tree policy: refuse direct when a changed path OVERLAPS operator-dirty
  paths captured at run start, unless --force. Clean-elsewhere is fine.
- TDD: deny-glob write refused; escape-above-repo refused; dirty-overlap refused
  without --force; verify_enforcement hard-fails closed; clean direct run edits
  the real tree and passes. Then Grok-implement behind a contract.

### Task 7.2 - one-time consent + setup default (companion)
Files: companion-setup.mjs, lib/jobs.mjs (prefs), skills/setup, README.
- First direct run without a recorded consent -> fail closed with a one-screen
  trust summary and the exact setup command to accept (integration=direct).
  Silent default flip is refused. Consent is sticky per workspace.
- setup --integration direct|auto|review persists the default; userConfig
  integrationMode option added (Claude manifest).
- TDD (fake-wrapper harness): no-consent direct refused with the summary;
  post-consent direct proceeds; auto/review need no consent.

### Task 7.3 - auto mode: apply-on-verified-ready with apply-time revalidation
Files: companion (new lib/integrate.mjs), code/implement dispatch, tests.
- On dual-condition READY: re-read readiness, git apply --check --binary,
  capture operator-tree precondition, apply; on ANY failure STOP and report a
  PARTIAL state honestly (no "ready-applied" claim without the recheck). Prefer
  a single atomic apply; document a reverse-patch rollback path.
- TDD: tree mutated between ready and apply -> refuses/rolls back, never
  half-claims; clean path applies and reports the applied files.

### Task 7.4 - ACP un-gimp (evidence-backed) + default peer channel
Files: modes/peer_finalize.py, peer.py, grok_agent.py, agents/*, skills/peer,
grok-companion.mjs, tests.
- peer-stop runs the contract's requiredValidation + build gate FOR REAL via
  wrapper-executed commands; ready=true ONLY from that non-forgeable evidence
  (no preview-rewrite path may set ready; model claims ignored). Integrate via
  the active mode (direct: already live; auto: apply; review: patch).
- grok-engineer-coder DEFAULTS to the live multi-turn ACP peer; code = fallback.
  GROK_EXPERIMENTAL_ACP drops from hard gate to opt-OUT for one-shot.
- TDD: ready=true requires real exit-0 evidence (forge attempt fails);
  validation-fail -> not ready; peer result integrates per mode.
- Adversarial re-review of peer_finalize after (the forgery came back once; do
  not trust it green without an attack pass).

### Task 7.5 - docs sweep, mode-aware + honest (DRY)
Files: new plugin/references/integration-modes.md (single source); every
"never auto-apply" / "parent apply is manual" statement in README, skills,
agents, references, CHANGELOG becomes mode-aware and REFERENCES that one doc
(no copy-paste). SECURITY.md states plainly: direct-default = trusted-input,
isolation is GONE in direct, private home does NOT protect operator .env.
Remove any claim that hardened-direct keeps the tree sandbox-safe.

### Task 7.6 - DRY closeout
- tools/gen-manifests.mjs: single source -> writes both plugin.json manifests +
  marketplace version fields; run in CI + pre-commit; drift test stays the guard.
- Consolidate remaining companion test fixtures onto helpers/fake-wrapper.mjs
  (kill "pending consolidation").

### Task 7.7 - the real peer-agent-path live smoke (the dogfood skipped in 2.0)
- Spawn the ACTUAL grok-engineer-coder SUBAGENT (Agent/Task path, not the raw
  companion CLI) and delegate a genuine small feature in direct mode. Verify:
  edits land in the tree, deny-globs refuse a .env write, dirty-guard refuses an
  overlap, consent gate fired first-run. Record in the ledger. This is the
  workflow the whole project is FOR and was never exercised.

### Task 7.8 - final full-branch pre-tag review + CHANGELOG
- Re-run the full-branch adversarial review (it caught the last release-blocker).
- CHANGELOG 2.0.0: integration modes, ACP-default, honest direct-default posture.
- Then the branch is genuinely tag-ready; tag stays the maintainer's call.

## Guards that MUST hold (review non-negotiables)
1. direct default is consented, not silent. 2. dirty-overlap refused without
   --force. 3. in-repo deny-globs (.git/.env/keys/hooks) enforced + scanned.
4. verify_enforcement hard-fails for write/integrate modes. 5. auto-apply
   revalidates at apply time. 6. ready=true only from non-forgeable wrapper
   evidence. 7. no doc claims the sandbox protects the tree in direct. 8. raw
   never default, never aliased as direct.
