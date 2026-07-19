# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for marketplace / package tags.

## [2.0.0] - 2026-07-17

Peer-agent integration release (plan: docs/superpowers/plans/2026-07-16-peer-agent-integration.md).
Phases land as sequential PRs; no tag until the release phase. Most implementation
is delegated to Grok itself through this plugin's own contract -> code -> handoff
pipeline; live evidence in docs/checklists/2.0-live-smoke-ledger.md.

### Added (Phase 0 - hygiene, PR6)

- `tools/verify.sh` + `tools/checks.sh`: one-command verification gate (both unit
  suites, 900-line cap with ratcheting `tools/cap-allowlist.txt`, ASCII-hyphen
  check, `claude plugin validate --strict`). CI gains a `mechanical` job.
- Fake-wrapper test harness for companion tests
  (`plugin/scripts/tests/helpers/fake-wrapper.mjs`): the canonical harness for
  NEW companion tests (none spawn the real wrapper or the Grok CLI); older
  suites still carry their own fixtures pending consolidation.
- `plugin/references/argv-safety.md`: single canonical injection-safety
  reference (task text via stdin heredoc, single-quoted flag values, contract
  `requiredValidation` argv is shell-free); six skills now carry a short inline
  summary instead of duplicated rationale.
- AGENTS.md/CLAUDE.md divergence warning: permissive rules discovery reports
  when a level's pair differs and only AGENTS.md was sent
  (`discover_instruction_files_with_warnings`; `ModeRun.initial_warnings`).
- `docs/checklists/2.0-live-smoke-ledger.md`: append-only live-behavior
  evidence for the 2.0 work.

### Added (Phase 1 - delegation, PR7)

- **`/grok:implement`**: one-call delegate cycle - `code` then automatic
  `/grok:handoff` verification on the resulting runId; both envelopes relayed
  in order; exit 0 only on dual-condition ready. Handoff runs even after
  failed code (with a runId) so blockers surface. Verify-only (does not
  apply); for apply-on-ready use `code --integration auto`. Hardened runMode
  only; runMode direct refuses fail-closed. Mode matrix:
  `plugin/references/integration-modes.md`.
- Contract steering: `--contract-file` objective, acceptance criteria, write
  scopes, and validation commands are injected into Grok's prompt
  (single-line fenced, data-not-instructions framing); a display-only
  `contractSummary` (size-capped, display-redacted) rides the handoff
  manifest and response so parents can check criteria before applying.
- Agents derive a contract by default (`grok-engineer-coder`, both hosts),
  with live-lesson guardrails: shell-free `requiredValidation` argv, targeted
  test modules, no checkout mutation while a run is in flight, secret-fixture
  patch limitation.
- Unified ids: `result`/`status`/`cancel` accept a job id or a runId (exact
  job-id match wins; `status` translates a known job id to its recorded
  runId). Direct-mode runs join the job surface; `handoff`/`status` refuse
  `direct-*` ids with one actionable message.

### Added (Phase 2 - iteration loop, PR8)

- **`code --continue-run <runId>`**: iterate on a completed code run instead
  of starting over. Reuses the retained worktree, resumes the archived Grok
  session (`--resume`, probe-verified), re-applies the persisted contract,
  and mints a NEW run with `iteration`/`continuesRunId` lineage in run.json
  and the handoff manifest. `--target/--base/--contract-file` are derived
  from the prior run and refused alongside `--continue-run`.
- Per-run session archive: the private home's Grok session store is copied
  into the run dir before teardown (0700/0600; contains prompt history; see
  SECURITY.md "Session archives"); seeded back on continuation. Archive
  failure warns, never flips a run.
- Continuation hardening: single-lineage chains (`continuedByRunId` CAS
  guard forbids forked siblings and concurrent writers), persisted-contract
  integrity pinned to the prior `contractSha256` (missing or tampered fails
  closed), prior base verified at entry, `MAX_CONTINUATION_ITERATION` = 20.
- Cleanup semantics for chains: a continuation cleans its own run state and
  defers the shared worktree to its owning run (note, not failure); missing
  worktree is a note for continuations; foreign ownership mismatches still
  fail closed.
- Live end-to-end evidence (ledger): seed run wrote `alpha`; continuation
  recalled the prior turn, appended `beta` in the same worktree, and its
  handoff was dual-condition ready.

### Added (Phase 3 - Claude Code native surface, PR9)

- **`grok-skills` bin shim**: plugins' auto-discovered `bin/` puts one bare
  command on the Bash tool's PATH (Claude Code only); agents invoke shim-first
  with the self-locating `run.mjs` fallback everywhere else.
- Persistent state honors `CLAUDE_PLUGIN_DATA` (atomic complete-marker
  migration including job bodies and gate state; retryable partials; legacy
  root stays a frozen snapshot).
- `userConfig` in the Claude manifest (runMode, notificationMode, sensitive
  webhook URL); values reach the companion as `CLAUDE_PLUGIN_OPTION_*` env
  with precedence explicit setup > userConfig env > defaults.
- SubagentStop handoff nudge (non-blocking, read-only): on
  grok-engineer-coder stop, advisory context names the run's handoff target,
  correlated from runIds in the agent's last message (validated) with a
  workspace-newest fallback. Codex note: plugin hooks stay dormant until
  trusted via /hooks.
- Agent frontmatter uses verified-honored keys (maxTurns 40, memory project;
  model stays inherited - review reversal: the agent is an orchestrator).
- Companion test suites are fully isolated (fresh XDG/TMPDIR/data/cwd per
  spawn); the shared-state concurrent-suite flake was root-caused (leak tests
  scanning the shared TMPDIR) and eliminated - proven with 3x-concurrent
  batches. A separate load-induced spawn flake (EAGAIN under heavy
  concurrency) was made resilient with a bounded spawn retry. Validation blockers now name failing tests (TAP not-ok
  extraction).
- Deferred by decision: plugin `subagentStatusLine` (overriding all subagent
  rows to annotate Grok jobs is over-reach for 2.0.0).

### Added (Phase 4 - Codex parity, PR10)

- `setup --codex-agents-scope user|project`: managed agent TOMLs can install
  into the project's `.codex/agents/` instead of `~/.codex/agents/`.
- Managed-agent backups capped at 3 (user files never touched);
  `nickname_candidates` on both Codex agents.
- Trust honesty: README/setup state plainly that Codex plugin hooks (stop
  gate + SubagentStop nudge) are dormant until trusted via `/hooks`;
  `docs/COMPATIBILITY.md` gains an upstream-gaps table (codex#18988/#18308).
- Validation blockers and command evidence name failing tests from FULL
  stdout (`failedTests`), surviving tail truncation.

### Added (Phase 5 - ACP peer channel, PR11)

- **ACP peer channel** (hardened only; wrapper + companion): `peer
  start|prompt|stop` drive a long-lived `grok agent stdio` (ACP) session with
  start parity (sandbox policy/profile, tool allowlist, no .env, private-home
  posture, baseline capture) before the first prompt. Start neither plants nor
  verifies the cwd sentinel; the first prompt instructs the model to create it,
  and peer-stop requires sentinel proof only when `promptsHandled > 0`.
  Wrapper-owned 0600 control socket (not a FIFO);
  peer.json records wrapper+child pid/starttime and the start
  `originalBaseline`; run.json records `worktreePath` / lifecycle so
  `cleanup --run-id` can remove the external worktree after peer-stop
  terminalizes. Streamed chunks and control-socket payloads are secret-scanned
  (same guarantee as `emit_envelope`). Resident peer-start emits **exactly one**
  stdout envelope (`running`); peer-stop emits the terminal outcome.
- **Evidence-backed peer-stop finalize (Task 7.4):** peer-stop runs contract
  `requiredValidation` and the wrapper build gate **for real** (same ordered
  finalize as code; exit_status never synthesized). `integration.ready=true`
  only when an authoritative validation source passed and `commands[]` carries
  a real `exitStatus` (forgery guard fails closed). No authoritative gate ->
  honest `no-authoritative-validation` blocker. Ready peer results integrate
  via the active mode (auto/direct apply verified patch; review leaves patch),
  applied by `peer stop` itself. `/grok:handoff` stays code-mode only and
  refuses peer runIds (see the Fixed note below). ACP is the **default** peer
  channel for `grok-engineer-coder`; `GROK_DISABLE_ACP=1` is the opt-out
  (`GROK_EXPERIMENTAL_ACP` is no longer a hard gate). Crash-path peer-stop
  reuses the start baseline (never re-captures). Optional sandbox
  `verify_enforcement` at stop records failure honestly. New `acp-failure`
  error class; reaper respects live peer homes. Design:
  `docs/specs/2026-07-17-acp-peer-channel-design.md`.

### Added (Phase 7 - peer-native integration modes + docs)

- **Integration modes** (orthogonal to runMode security): `direct` (DEFAULT),
  `auto`, `review` (+ `worktree` isolation alias). Canonical reference:
  `plugin/references/integration-modes.md`.
- **direct default (hardened-direct):** under runMode hardened, Grok edits the
  operator's real tree with private home + sandbox-to-repo + redaction;
  protected paths (`.env`/keys + nested/modules/in-workspace-gitfile sensitive
  git metadata) deny-scanned and rolled back best-effort; source edits land
  live; one-time `setup --integration direct` consent per target repo
  (env/userConfig alone never counts).
- **auto:** isolated worktree + dual-condition ready, then companion
  apply-on-verified-ready with apply-time revalidation (never half-applies).
- **review:** worktree + patch + manifest; never auto-applies (manual parent
  apply after ready handoff).
- **ACP default peer channel** for `grok-engineer-coder` (multi-turn
  start/prompt/stop; always external worktree); one-shot `code` is the fallback
  (`GROK_DISABLE_ACP=1`). Peer-stop lands via the same mode names with a different
  isolation story than code direct (stop-time apply for direct/auto; see Fixed
  residual note).
- **Docs honesty + DRY:** README top-line pitch matches direct-default;
  SECURITY states trusted-input posture for integration=direct; skills/agents
  link the single integration-modes reference (no bare "never auto-apply"
  absolute claims). Naming disambiguates runMode direct vs integration direct.

### Fixed / hardened (Phase 7 - final review remediation)

- **Direct protected git scope (nested / modules / in-workspace gitfile):**
  snapshot, restore, and git-dir guard cover root `.git`, nested workspace
  gitdirs (`vendor/.../.git`), `.git/modules/**`, and in-workspace gitfile
  targets (`gitdir:` under the repo). Logical keys (`.git/HEAD`, hooks, refs)
  resolve to the **actual** absolute gitdir - never write/delete under a
  gitfile path. Snapshot persists a `git_roots` prefix map so restore still hits the original
  common dir after a post-run pointer rewrite. Guard detection unions baseline
  roots with live discovery (new in-workspace redirect plants fail closed);
  gitfile pointer content is fingerprinted (external redirect not silent;
  pointer bytes outside auto-restore). `modules/**` inventoried under every
  discovered abs gitdir (root/nested free-standing and gitfile targets).
  Submodule aliases retain every logical gitfile prefix even when abs is
  already seen; restore prefers `abs_paths` then baseline roots (marker key
  never maps to target dir). Bounded no-symlink discovery fails closed on
  overflow. External linked common dirs remain outside full inventory.
  SECURITY direct-default item 3 documents the honest limit. Node
  `parseDiffGitHeaderPaths` dual-condition parity + bytes-safe
  `git_ignored_paths` land in the same pass.
- **Codex marketplace root drift (DRY hole):** `tools/gen-manifests.mjs` now
  generates and `--check`-guards BOTH marketplace roots. Previously only
  `.claude-plugin/marketplace.json` (version fields) was covered, so
  `.agents/plugins/marketplace.json` stayed hand-owned and its description
  drifted to stale pre-2.0 wording while the guard reported OK. Both roots now
  source description/keywords/displayName from `plugin/manifest.source.json`;
  new parity tests prove the Codex root is guarded and the retired wording
  cannot reappear.
- **direct-mode `.git/index` false-positive (dogfood-caught):** the git-dir
  guard no longer treats `.git/index` / `.git/COMMIT_EDITMSG` as protected-path
  writes. git rewrites the index on ordinary reads (`git status`), so guarding
  it failed essentially every real direct run at finalize (a live
  `grok-engineer-coder` subagent dogfood hit this: Grok's `src/` edits + passing
  tests were discarded because a `git status` had touched the index). Only the
  security-relevant `.git` set (config/HEAD/packed-refs/hooks/refs) is guarded.
- **direct-mode `.git/refs` guard + rollback:** the git-dir guard now
  fingerprints `.git/refs/**` and the protected snapshot covers refs +
  `.git/packed-refs`, so a direct-mode branch/tag move-to-planted-commit or a
  created ref is detected and reverted/removed. `.git/index` stays detect-only
  (git rebuilds it); loose `.git/objects` are untracked (content-addressed,
  inert until a watched ref points at them) - now stated honestly in SECURITY.md
  and the module headers instead of a blanket "`.git/**` rolled back" claim.
- **Expanded deny-list:** `*.p8`, SSH private keys (`id_rsa`/`id_dsa`/`id_ecdsa`/
  `id_ed25519`), `.netrc`, `.npmrc`, `.envrc` are deny-scanned and rolled back.
- **Peer-stop apply parity:** `maybeIntegratePeerStop` now reverses (`git apply
  -R`) on a failed apply so a peer integration never leaves a half-applied tree,
  matching the auto path.
- **Consent copy honesty:** direct-integration consent text lists the actual
  covered protected set (`.git` config/HEAD/hooks/refs, `.env`, key files)
  rather than an unqualified `.git`.
- **ACP doc drift:** SECURITY.md and `manual-smoke.md` no longer describe the
  peer channel as experimental/never-ready/`GROK_EXPERIMENTAL_ACP`-gated; they
  match the shipped default (opt out with `GROK_DISABLE_ACP=1`, real validation,
  peer-stop applies per integration mode). `integration-modes.md` no longer
  claims `/grok:handoff` observes peer-stop ready - handoff stays code-mode only
  and refuses peer runIds (`handoff-unavailable`).

### Fixed (Phase 7 - peer-stop final-envelope completion path)

- **Peer-stop completion honesty:** a ready wrapper envelope whose apply is
  blocked (consent-required, dirty-overlap, integrity, etc.) no longer leaves
  stdout / `/grok:result` storage as raw `status: success`. Companion captures
  wrapper output, runs peer integration, and attaches the final outcome via the
  shared auto final-envelope SSOT (`attachIntegrationFinalOutcome` /
  `buildPeerStopFinalEnvelope`) under rewrite-before-write/store/finalize:
  onStdout computes final emitStdout/effectiveCode before first write; then
  stdout write; then storeJobStdout; then updateJob/finalize; then notify.
  Exactly one final envelope is emitted and stored; the job is finalized from
  that envelope + effective code. Success apply still one success envelope with
  `response.integration.applied=true`. Peer-stop remains **outside**
  completion-notification eligibility (`NOTIFY_ELIGIBLE_MODES`); the rewrite
  path does not invent peer toasts.
- Capture path extracted to `lib/companion-capture.mjs` so the entrypoint stays
  under the 900-line cap while the rewrite-before-write/store/finalize contract
  is centralized.

### Fixed (Phase 7 - post-round-14 apply / peer / path honesty)

Consolidated by root cause (not review-round chronology). Unit coverage lands
with each behavior; installed-host live smoke remains deferred to dual-host
post-smoke (see live-smoke ledger).

- **Shared fail-closed apply spine (auto + peer):** one dirty-guard ladder for
  both `code --integration auto` and ready peer-stop apply - `git status
  --porcelain -z` fail-closed, `git apply --numstat` fail-closed, dirty-overlap
  block, `git apply --check --binary`, apply, reverse-on-failure. Outcomes
  include `blocked-dirty-status` / `blocked-numstat` / `blocked-dirty-overlap`
  (no blind apply when status cannot be trusted).
- **Patch integrity recheck before apply:** after apply-time handoff ready,
  auto rechecks `implementation.patch` bytes/size/hash against the revalidated
  manifest (`verifyPatchAgainstManifest`, same SSOT as peer) and fails closed
  with `patch-integrity-failure` if substituted/corrupted. Best-effort under
  trusted local state - not an atomic TOCTOU seal.
- **Complete auto failure envelope:** when auto has no usable handoff envelope,
  emit one full C4-shaped failure envelope (`schemaVersion`, `mode=code`,
  `status=failure`, stable `runId` or null, classified error,
  `response.integration.applied=false` / `ready=false`) instead of a partial
  schemaVersion/mode/status-only object. Never invent success. Classes:
  empty stdout => `output-missing`; non-JSON => `output-malformed`; parseable
  code envelope missing usable runId => `handoff-unavailable` (not
  `output-malformed`). Integration outcome fields route through
  `attachIntegrationFinalOutcome`.
- **NUL-safe path inventory + quoted patch headers:** shared `path_inventory`
  uses raw `-z` relative paths so default `core.quotePath` non-ASCII names no
  longer become phantom dirty keys; handoff path cross-check decodes C-quoted
  `diff --git` headers via `git_path_quote` (octal + named escapes, a/b sides,
  `/dev/null`) without C-unquoting NUL-safe inventories. Companion
  `unquoteGitPath` shares golden vectors in
  `plugin/references/git-c-quoted-path-vectors.json` (Python + Node parity
  tests; no runtime cross-language dependency).
- **Last-wins task/web argv (companion-args SSOT):** split-or-equals
  `flagValue` last-wins for `--task` / `--task-file` (and stdin sentinel
  staging); `--web` / `--no-web` resolve by last occurrence (equals-aware,
  prefix-safe). Task-file-over-task policy, hermetic force-off, schema refusal,
  and D-WEB expansion preserved.
- **ACP child C6 pins + capability-only pre_tool_use honesty:** peer
  `agent stdio` spawn assembles the same global tool / permission / web /
  sandbox pins the running envelope advertises (`code._TOOLS` +
  `effective_tools` + `HEADLESS_PERMISSION_MODE`, probe-accepted global
  placement before `agent`). Initialize may advertise `pre_tool_use` as a
  capability; the wrapper does **not** register a deny hook - OS sandbox + C6
  pins are the real layers (documented NON-enforcement).
- **Peer-stop single-flight lifecycle + field-safe peer.json:**
  running -> stopping -> stopped|failed under `run_lock` with `stopOwner`
  identity, abandoned-stopping reclaim after grace, concurrent field updates
  not clobbered, failure ends as `failed` (not `stopped`), concurrent dual-stop
  finalizes once, durable terminals restop without revalidation, live-wrapper
  fallback kill only on confirmed pid+startToken. Centralized peer.json RMW
  refuses empty stopOwner tokens while PID is alive and refuses peer-prompt for
  non-promptable lifecycles.
- **Peer durable-terminal reclaim + denylist reload:** terminal `peer.json`
  without a loadable durable envelope is reclaimable (not forever poison);
  `_terminalize_peer_run` reports durable success only when envelope evidence
  exists (already-terminal bare run records return false). Local/crash
  peer-stop reloads injected exact-value secrets from private-home
  `auth.json` before patch scan/envelope/destroy; register preserves a
  non-empty resident denylist when home extraction is empty; trustworthy
  non-empty homes still replace; empty process + missing home stays
  pattern-only.

### Fixed (Phase 7 - PR #5 Codex code-review remediation)

Verified each inline review comment against current code (many were already
resolved or stale after the re-architecture); fixed the genuinely-valid ones in
three batches. Two release-blockers were real.

- **Direct-mode rollback on abnormal exit:** protected-path rollback now runs on
  any abnormal Grok exit / gate-or-validation failure / realpath-escape (not just
  finalize's happy path); deny-scan covers `.env/**`; continue-run on a direct
  run gives an honest hint.
- **Peer/ACP (blockers + lifecycle):** the ACP child now runs under the same
  global `--sandbox <profile>` confinement and minimal env (HOME/PATH/TMPDIR) as
  code mode (was: unsandboxed + full operator-env passthrough while the envelope
  claimed confinement); private tmp unified to `<home>/tmp` (two-phase, no leak);
  peer-stop fails closed on auth-teardown failure; fallback kills the orphaned
  child (fail-safe, positive-identity + never-own-group); stdout-suppress armed
  before serving; strict UTF-8 ACP frame decode; companion peer-start exits
  nonzero unless status=running; start-parity test no longer breaks a clean CI.
- **Companion correctness/security:** continue-run exempt from direct consent;
  `parseTargetFlag` last-wins (matches wrapper); setup bare-mode scan skips flag
  values; session archive skips symlinks; contiguous secret-shaped test literals
  split (AGENTS.md #8); handoff-consumed marker now written; auto-apply blocks
  dirty-overlap; legacy prefs pinned as setup only when non-default.
- **Peer start-abort cleanup + model-created sentinel:** a peer-start that fails
  after creating the worktree/home/child now tears all of them down and
  terminalizes the run (no orphaned credential home / worktree / process); the
  cwd sentinel is created by Grok on its first prompt (not planted by the
  wrapper), making the stop-time proof genuine, with a zero-prompt session
  exempted from the sentinel check.
- **implement/auto job + notification** are finalized after handoff/apply, so a
  not-ready implement no longer reports a premature success job or notification.
- **Re-review rounds 2-7 hardening** (the bot re-scanned every commit): the
  implement/auto combo notifies with a notify-eligible mode and never notifies
  success on a failed apply; `extractTask` / `injectTaskFile` / stdin-sentinel
  staging accept the equals-form `--task=` / `--task-file=` / `--task-file=-`;
  a `code --continue-run` in a `runMode=direct` workspace routes to the hardened
  wrapper (retained lineage) instead of a live direct edit; peer-stop fails
  closed (nonzero exit) when a requested direct apply is blocked by missing
  consent; auto's apply-time revalidation no longer emits a second stdout
  envelope; the durable peer-start stderr log and the direct-mode prompt file are
  created `0600` (were world-readable under `/tmp`); direct mode honors
  `--timeout` / the per-mode defaults so a hung installed CLI cannot block the
  companion forever; direct-mode installed-CLI stderr is redacted through the
  single-source redactor before it reaches the terminal; the dirty-overlap guard
  no longer mis-parses a literal `->` in a filename; direct `reason` refuses
  `--input` / `--rules-file` rather than silently dropping them; and
  `/grok:handoff` is documented consistently as code-mode only (peer runs
  integrate via `peer stop` itself).

Every applicable review finding was fixed (no deferrals). Comments that were
already resolved or stale after the re-architecture are noted as such above.

### Fixed (Phase 7 - docs/runtime honesty residual)

- **Start parity sentinel honesty:** `plugin/skills/peer/SKILL.md`, the ACP
  peer-channel design spec, and Phase 5 changelog wording now match
  `peer_process.assert_start_parity` exactly - start verifies sandbox
  policy/profile, tools, `.env` absence, and private-home posture; it neither
  plants nor verifies the cwd sentinel. The first peer prompt instructs the
  model to create the sentinel; peer-stop requires sentinel proof only when
  `promptsHandled > 0`.
- **ACP `clientInfo.version`:** initialize advertises packaging-stable `2.0.0`
  (not `experimental`); unit coverage asserts the initialize payload.
- **implementation-handoff checklist:** trailing whitespace removed from the
  parent-apply numbered list (`git diff --check` clean).
- **Peer integration docs-follow-code:** product surfaces no longer group ACP
  peer `integration=direct` with one-shot code live-tree direct. Runtime truth:
  peer always uses an external retained worktree; at ready peer-stop,
  `direct`/`auto` both apply the verified patch (direct needs consent) and
  `review` retains. Canonical matrix + peer skill, SECURITY, peer-native design,
  README/COMPATIBILITY/agents/manifests/manual-smoke updated without rewriting
  frozen plans/ledger rows.

**Round 2** (the review bot re-scanned each commit and found further issues,
including regressions in the round-1 fixes): direct git-guard now content-hashes
watched files (a same-size ref move with restored mtime no longer evades) and
resolves linked-worktree git dirs; protected rollback preserves pre-existing
symlinks and file modes; the wrapper's bare-call integration default is the safe
`worktree` (product direct-default unchanged - the companion passes it explicitly
after consent); direct-mode state nested in the target repo fails closed;
runMode=direct envelopes are redacted through the wrapper's single redaction
source; peer-stop apply keys consent on the peer repo, blocks dirty overlap
(both rename sides), and fails the command on a failed apply; implement always
routes to worktree; the stale-home reaper never reaps a live-owner peer;
peer prompts carry the repo rules + contract; peer-prompt failures keep the run
id; peer-stop fails closed if it cannot persist terminal evidence; the resident
peer wrapper uses an ignored stderr fd; agent recipes use a valid mktemp template
and quote the contract path. Suite: wrapper 809, plugin 285.

### Fixed (Phase 7 - code worktree / direct landing docs honesty)

- **Mode-aware code landing docs:** product surfaces no longer claim one-shot
  `code` always writes only in an external worktree. Truth: consented product
  default is live-tree `integration=direct` (hardened-direct sandbox-to-repo);
  bare wrapper still defaults to worktree; auto/review stay external worktrees;
  ACP peer remains always-worktree with stop-time apply. Updated README Security
  short section, wrapper `SKILL.md` code section, `authority-policies.md` code
  row, `modes/code.py` header comments, `manual-smoke.md` scenarios, peer-native
  design (removed phantom `--integration direct --raw`; runMode is a separate
  axis only), plus adjacent workflow-patterns / roadmap hardened-mode wording.
  Preserves recent peer direct stop-time-apply corrections; frozen plans/history
  untouched.
- **manual-smoke peer `--integration` placement:** `--integration` is a
  per-invocation companion flag and peer-start does not persist it. The ACP
  peer live-smoke recipe now puts `--integration review` on `peer stop` (the
  command that determines retain vs apply) and omits it from `peer start`.
  Peer skill / agents already matched product surfaces; only the smoke recipe
  was wrong.

### Fixed (Phase 7 - final-review apply lock / argv / peer lifecycle / docs)

Consolidated final-review remediation against code at `3fca5a4` (not per-round
spam). Unit contracts land with each behavior; dual-host installed-host smoke
remains release-gated.

- **Exclusive apply lock + durable marker (auto + peer):** per-`(runId,
  targetKey)` atomic mkdir lock + durable `owner.json` (`pid`/`startToken`/
  `acquiredAt`); reclaim only positively dead owners after settle; ownerless /
  unknown never age-reclaim; owner write fail removes lock dir and fails closed.
  Durable `integration-applied-<targetKey>.json` (patchSha + targetKey; tmp +
  rename + re-read). Under-lock ladder: matching marker + reverse-check =>
  already-applied; marker but reverted tree => clear + reapply; no marker but
  reverse OK => revalidate under lock then heal marker; else revalidate, apply,
  finalize with marker (marker fail after apply => reverse =>
  `marker-persist-failure` or `manual-needed`). Honesty: not a TOCTOU seal;
  abandoned ownerless locks need manual cleanup.
- **`loadPatchTouchPaths` header fail-closed:** union numstat +
  `diff --git`/rename-copy both sides; non-empty numstat makes headers
  load-bearing (`blocked-patch-headers` when empty/unparseable/uncorroborated);
  pure renames put both old and new in the dirty-overlap set.
- **Last-valid companion argv SSOT:** `flagValue` is last **valid** split/equals
  value; later bare without value does not wipe; never consume a following flag
  as value; `resolveWebFlag` last occurrence. continue-run forbids
  `--target`/`--base`/`--contract-file`; prior-run target identity; continue-run
  consent exempt; direct continue uses hardened wrapper for retained lineage;
  auto apply-on-ready on the **new** run; review manual.
- **implement always worktree + verify-only:** companion gate always forces
  implement to isolated worktree + handoff; never live lands even when workspace
  integration is direct/auto. Product direct default remains for **code** and
  **peer-stop** landing.
- **Peer lifecycle honesty:** durable terminal before restop success; mandatory
  `promptsHandled` persist; `startToken` identity fail-closed; control frame
  caps (~4 MiB); active-proc never pid-scan-unregister on kill refusal; peer-stop
  not completion-notification eligible.
- **Security honesty:** direct `REDACT_SCRIPT` loads D4(a) operator-auth exact
  denylist; protected content-hash includes ignored protected paths + nested
  hooks; patch injected denylist scan; path inventory bytes + surrogateescape;
  direct sandbox limits unchanged.
- **Docs-follow-code:** `integration-modes.md` SSOT expands Shared apply spine +
  implement force-worktree + continue-run; README First 5 minutes requires setup
  consent or explicit isolation before promising implementer success; SECURITY /
  COMPATIBILITY / argv-safety / handoff / manual-smoke / skills link SSOT
  without copying tables.

### Changed (Phase 0)

- `plugin/scripts/lib/task-file.mjs`: task-text temp staging deduplicated
  (single owner for the stdin `--task-file -` path and companion injection).
- Oversized test files split by responsibility (`test_redaction.py`,
  `test_worktree_escape.py`, `test_handoff_patch.py`,
  `worktree_test_base.py`); the 900-line cap allowlist is empty.
- Prose/comments normalized to ASCII hyphens (AGENTS.md rule 12) outside dated
  evidence archives; enforced by `tools/checks.sh`.

### Suite counts (ratchet)

- Wrapper: 653 -> 809+. Plugin: 172 -> 285+ (through PR #5 code-review remediation + post-round-14 apply/peer/path honesty; exact counts re-verified by `tools/verify.sh` / CI).

## [1.6.0] - 2026-07-16

### Added

- **Verified implementation handoff (PR4):** optional `--contract-file` on `code`
  (writeScopes, requiredValidation argv, operator-trusted / no OS FS sandbox claim).
- Ordered post-Grok finalization with unexpected-commit and scope blockers;
  phase-1 immutable `git-binary-full-index-v1` patch; phase-2
  `implementation-handoff.json` with `integration.ready` from in-memory
  `terminalOutcome`.
- **`/grok:handoff --run-id`:** read-only dual-condition ready (manifest + success
  `mode:code` envelope with matching non-null `baseRevision` + non-empty patch
  size/rehash). Companion thin passthrough (no job/notify/Grok).
- Command evidence: sha256 + 4096 redacted tails on wrapper commands.
- Skills/agents dual-host parent protocol (notify ≠ ready; never auto-apply).
- Cleanup factual warning when removing an integration-ready handoff.

### Changed

- Packaging triple **1.6.0**.
- Seven new ERROR_CLASSES for handoff/contract.
- Dual-condition / ready integrity: require non-empty envelope base; ready
  manifests need `patch.bytes > 0` when there are changes; Git paths keep colon
  filenames; post-gate fatal patch capture clears stale pre-gate patch/tree meta.
- Adversarial integrity pass: `list_changed_paths` fail-closed on git fatal;
  post-gate list/scope refresh hard-fails; unlink stale patch on reject;
  latin-1 patch secret scan; pre-gate patch fatals superseded by clean post;
  unknown blockers hard; `resultTreeOid` never a commit SHA; argv redaction in
  command evidence + spawn logs; `write_manifest` secret scan; `groklib.redaction`
  extract (envelope under 900 lines); parent docs dual-condition + recipe 14.
- Dual-condition also cross-checks `changedFiles` against patch `diff --git`
  paths and (when present) the code envelope's `changedFiles` list.

## [1.5.0] - 2026-07-16

### Added

- **Completion notifications (companion):** optional push when a live run finishes.
  Jobs config: `notificationMode` (`off` default, recommend `auto` for background),
  `notificationWebhookUrl`. At-most-once attempt via `notified.json` (no auto-retry,
  not exactly-once). Native OS notify (macOS/Linux) and webhook POST.
- **Execution context:** skills/agents set `GROK_COMPANION_EXECUTION_CONTEXT` to
  `foreground` or `background` (never forwarded to the wrapper). `auto` notifies
  only for background.
- Setup flags: `--notification-mode`, `--notification-webhook-url`.

### Changed

- Packaging triple **1.5.0**.

### Fixed

- **Isolation (post-PR2 Codex follow-ups):** owner marker written before
  `git worktree add`; dirty patch vs pinned base SHA (not live HEAD); retain
  owner marker if worktree still present after cleanup; ITA OIDs any all-zero
  length (SHA-256); status porcelain captured as bytes for non-UTF-8 paths.

## [1.4.0] - 2026-07-16

### Added

- **Opt-in isolated review (`--isolated`):** when set, `review` runs in an owned
  external worktree under the state root (`worktrees/review/{run_id}`), applies
  tracked dirty (staged + unstaged) from the live checkout, and cleans up always.
  Intent-to-add (`git add -N`) and untracked files are excluded. Dirty submodules
  fail closed. Setup failures emit `isolation-unavailable` with no silent
  fallback to the live tree.
- **`--base` remains live:** comparison framing only; does not force isolation.
  Combine `--base` and `--isolated` when you want framing plus a snapshot tree.

### Changed

- Packaging triple **1.4.0**.

## [1.3.1] - 2026-07-16

### Fixed

- **Adversarial PR1 hardening:** terminal lifecycle only via envelope-first
  `persist_terminal_envelope`; `set_lifecycle` cannot terminalize; entrypoint
  never stores `envelope.json` (modes are sole durable writers); public
  `write_run_record` removed; preflight/terminalize fail-closed on persist
  failure; SIGTERM durable lifecycle `canceled`; unkillable finalize is
  stdout-only; success-on-stdout without durable terminal is fail-closed;
  status wall-clock `elapsedMs` + projection/regression tests.

## [1.3.0] - 2026-07-16

### Added

- **Durable run lifecycle:** seed `run.json` (lifecycle `created`, status
  `running`, `recordRevision` 0) before run-id publication; exclusive
  `run.lock` + compare-and-swap record updates; envelope-first
  `persist_terminal_envelope` (idempotent lifecycle finish; never replace a
  valid terminal envelope).
- **Spawn finalization worker** with parent recovery only when the worker is
  confirmed not alive (`finalization-timeout` /
  `finalization-worker-missing-result` / ephemeral `finalization-worker-unkillable`).
- Progress events carry process-local monotonic `elapsedMs` and UTC `ts`.

### Changed

- **`/grok:status` projection:** strictly read-only; effective lifecycle from
  record → valid envelope → derived `interrupted` (dead owner, no envelope).
  Failed/canceled/interrupted targets return top-level `failure` and exit 1
  while still emitting a well-formed status envelope. `response.target`
  includes `lifecycle`, `lifecycleSource`, and `elapsedMs`.

## [1.2.10] - 2026-07-15

### Fixed

- **`/grok:status` in-progress UX:** when a run is still going (no
  `envelope.json` yet, owner process alive), status returns top-level
  `"status": "running"` with `response.target` (elapsedSeconds, process,
  eventCount, lastEvent, recentEvents) instead of pretending success and
  warning about a missing stored envelope. Exit 0 for both `success` and
  `running`. Companion no longer re-dumps progress to stderr after the JSON
  (hosts that merge streams were gluing `[grok] …` onto the envelope).

## [1.2.9] - 2026-07-15

### Fixed

- **Docs matched code:** README, PROVENANCE, authority policies, wrapper SKILL,
  checklists, roadmap, live probe docs, and setup hints now match behavior - 
  no hard CLI pin, repo-agnostic targets, review drift as warnings, reason web
  default off, Codex `agents/run.mjs` / `GROK_AGENT_RUN`, dual-host surface.

## [1.2.8] - 2026-07-15

### Fixed

- **No hard Grok CLI version lock:** runtime accepts any working
  `grok --version`. `accepted-version.json` is last-validated maintainer
  evidence only (`enforcement: none`), not a user allowlist. Exact build
  mismatch no longer fails closed as `version-mismatch`.

## [1.2.7] - 2026-07-15

### Fixed

- **Entry-derived plugin root wins over stale env:** `skills/*/run.mjs`,
  `agents/run.mjs`, `grok-companion.mjs`, and SessionStart always bind
  `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` to the install tree they live in so a
  leftover env after marketplace upgrade cannot mix old/new wrappers.
- **Wrapper override is opt-in:** `GROK_AGENT_WRAPPER` is ignored unless
  `GROK_ALLOW_WRAPPER_OVERRIDE=1` (tests/advanced only).
- **Codex agent writes are atomic** (`*.tmp` + rename); SessionStart timeout
  raised to 30s so agent materialize is less likely to be cut off.
- **Incomplete-stop salvage hardened:** empty shells (`findings: []` /
  `findings: null` / placeholder-only) are not salvaged; turn-exhaustion only
  when the operator set `--max-turns`; schema-invalid structured on incomplete
  runs is cleared and warned on the envelope (`incomplete_warnings` →
  `warnings`); failure envelopes keep incomplete notes when `response` is kept.
- Docs scrub: SessionStart zero post-install, `agents/run.mjs` /
  `GROK_AGENT_RUN`, model-invocation note, unlimited max-turns defaults.

## [1.2.6] - 2026-07-15

### Fixed

- **No default max-turns:** review/reason/code/verify omit `--max-turns` unless
  the operator sets it. Unlimited by default (subscription Grok CLI).
- **Cancelled with findings is not a wipe:** if Grok stops with `Cancelled` (or
  hits an explicit turn budget) but produced text/structured output, the wrapper
  returns **success** with a warning and keeps findings (`response` populated).
- **Turn-cap as Cancelled:** when `--max-turns` is set, Grok's observed
  `stopReason: Cancelled` at the budget classifies as turn-exhaustion (or
  salvage if content exists), not a silent user-cancel.
- Capture `num_turns` from more end-event field shapes into classification.

## [1.2.5] - 2026-07-15

### Fixed

- **Agents aligned with skills:** shared self-locating `agents/run.mjs` (same
  `skill-run` family as `skills/*/run.mjs`). Claude agents use
  `$PLUGIN_INSTALL/agents/run.mjs`; Codex managed TOML injects absolute
  `GROK_AGENT_RUN` to that runner (not bare companion-only env).

## [1.2.4] - 2026-07-15

### Fixed

- **Transparent Skill-tool entry:** every skill ships `skills/<name>/run.mjs` that
  self-locates the plugin root from its own path and spawns the companion.
  Model contract is only `node "$SKILL_BASE/run.mjs" <mode> …` where `SKILL_BASE`
  is the Skill tool base directory (no env, no invented cache versions).
  Shared: `scripts/lib/skill-run.mjs`. Docs: `plugin/references/plugin-root.md`.
  Also: `resolve-plugin-root` helpers/CLI for tests and advanced use.

## [1.2.3] - 2026-07-15

### Added

- Release process checklist: [docs/RELEASE.md](docs/RELEASE.md) (linked from README, AGENTS.md, CONTRIBUTING.md)
- [plugin/references/plugin-root.md](plugin/references/plugin-root.md): never invent cache paths; Codex agent uninstall
- Setup `--remove-codex-agents` (managed agents only, with `*.bak` backups)

### Fixed / improved

- Claude agents: `tools: Bash(node:*)` only; clearer rescue vs engineer-coder routing
- Codex agent TOML: `sandbox_mode = "read-only"`, never-invent-paths, absolute companion
- Managed agent updates create backups before overwrite
- Injection tests cover all Claude agents + Codex TOML templates
- **Skills allow model invocation by default:** removed `disable-model-invocation`
  from all `/grok:*` skills so Codex (and Claude Skill tool) can invoke them.
  Users still control when Grok runs; slash commands keep working.

## [1.2.2] - 2026-07-15

### Fixed

- **Review UX: stop discarding finished reviews for purity checks.**
  - Tree drift during the run → informational warning only
  - Grok listing change-shaped JSON keys → informational warning only
  - Pre-run FS baseline capture failure → soft-skip; review still runs
  - Findings always kept when Grok completed successfully
  - `unexpected-edits` remains for `code`/`verify` worktree escapes only
  - Audit: `docs/reviews/2026-07-15-over-conservatism-audit.md`

## [1.2.1] - 2026-07-15

### Fixed

- **Zero post-install for Codex agents:** `SessionStart` auto-installs managed
  agents into `~/.codex/agents/` with an **absolute** path to `grok-companion.mjs`
  (no `PLUGIN_ROOT` required at spawn). *(Agent entry path superseded by 1.2.5:
  absolute `agents/run.mjs` / `GROK_AGENT_RUN`.)* Manual `/grok:setup` is optional
  (readiness / gate / mode only).
- Managed agents refresh when the plugin cache path or templates change; user-owned
  TOML (no `managed-by: grok-skills` header) is left alone unless `--force-codex-agents`.
- Setup exit code fails when agent ensure fails (unless `--skip-codex-agents`).

## [1.2.0] - 2026-07-15

First public release of **grok-skills**: dual-host Grok companion for Claude Code and Codex.

### Added

- Hardened Python wrapper (7 modes, envelope, progress stream, worktree isolation)
- Dual packaging: Claude marketplace + Codex `.agents` marketplace; wrapper under `plugin/wrapper/`
- Skills: preflight, setup, review, adversarial-review, reason, code, verify, debate, dual-lens, jobs, result, cancel, transfer, status, cleanup
- **Agents:** `grok-engineer-coder` (implementer; host orchestrates) and `grok-rescue` (diagnosis)
- Setup installs Codex agents into `~/.codex/agents/`; Claude loads `plugin/agents/` automatically
- Job registry, dual run modes (hardened / direct), optional fail-closed stop-review gate
- Preflight cache, citations, web defaults, transfer allowlist, workspace session stamps
- SECURITY.md, AGENTS.md, CONTRIBUTING.md, CI (Python 3.11/3.12, Node 20/22, packaging checks)

### Security

- Fail-closed stop gate (structured findings / verify pass; forces hardened)
- Progress redact-on-write; secret patterns + injected-auth denylist
- Gate-scripts-modified hard fail; git hooks disabled on worktree ops
- Honest trusted-input model and residual limits (D-SECRETREAD, D-NET, D3)
