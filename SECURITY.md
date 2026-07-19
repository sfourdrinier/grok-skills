# Security Policy

## What this project is (trust model)

`grok-skills` is a **hardened runner for trusted input** - code and repositories you are
willing to let the Grok CLI read, and (in `code` / `verify`) exercise build/test scripts
over. It is **not** a complete sandbox against an adversarial model.

### Enforced guarantees

- **Write confinement** for the sandboxed Grok child (verified OS sandbox profile).
- **Private auth home** per run (isolated `HOME`, 0600 credential copy, teardown).
- **Exactly one machine-readable result envelope** on stdout, scanned/redacted for
  known secret shapes and for exact injected credential values.
- **Fail-closed defaults** when the platform, sandbox, or stream cannot be
  verified (not when Grok CLI build string differs from last-validated stamp).
- **Worktree isolation** for one-shot `code` when **integration** is `auto` /
  `review` / `worktree` (external worktree + escape detection), and **always**
  for the ACP peer channel (external retained worktree for the whole session).
  **Not** claimed for one-shot **code** `integration=direct` (default live-tree
  path) - see [direct-default trust posture](#direct-default-trust-posture-integrationdirect)
  below. Peer `integration=direct` is stop-time apply, not that live-tree path.
- **Build-gate script integrity** (D1): gate scripts modified by the run are refused.

### Accepted limits (please read)

- The OS sandbox on the Grok CLI confines **writes**, not arbitrary **reads**
  (D-SECRETREAD). Absolute-path reads of host secrets are possible; network egress is
  permitted so the model can work as designed. Do not treat `deny_read_globs` as enforced.
- `verify` disables first-party web tools but still allows `run_terminal_command`; it is
  **not** a hermetic network sandbox.
- Pattern-based secret redaction (plus exact injected-auth denylist) is
  **defense-in-depth**, not a complete secret firewall. Finite patterns will miss novel
  credential shapes.
- Sandbox “enforced” evidence is derived from CLI telemetry the child can write; it is
  not a root-of-trust OS proof (see OPEN-SECURITY D3).
- `code` may run project scripts outside the sandbox after the model finishes (with a
  **hard fail** if gate scripts in package.json were modified by the run). Only point
  `code` / `verify` at repositories whose exposure you accept.
- Same-UID local malware can still read state under the operator account; 0700/0600 is
  not a multi-tenant security boundary.
- **Notifications (1.5.0+):** optional OS toasts and webhooks after terminal live
  runs. Default is **off**. Webhook URL is stored in the workspace jobs index
  (plugin state), not in the wrapper envelope. Payload is small
  (`runId`, `mode`, `lifecycle`, `durationSeconds`) - do not put secrets in the
  webhook URL. At-most-once **attempt** only (no delivery guarantee; no auto-retry).
  Native OS notify expects a **macOS/Linux desktop** session; it is **not**
  implemented on Windows (use **webhook**). Prefer webhook for SSH/CI/headless
  until PR5 setup/docs polish. Direct mode has no push notify in 1.5.0 (job
  still tracked; PR5 job-scoped marker). Isolation dirty patches may briefly
  exist as `*.diff` under the state root and are cleaned on success/cleanup
  paths; treat state root as sensitive.
- **Implementation handoff (1.6.0+):** optional `--contract-file` is
  **operator-trusted** content (not untrusted model output).
  `requiredValidation` argv runs with `shell=False` and cwd confined to the
  worktree (or target tree); there is **no** OS filesystem sandbox claim for
  those commands (they can write outside the worktree if the operator points
  them at a capable binary). **runMode direct** rejects `--contract-file` (fail
  closed). Handoff mode re-hashes the patch under the owned run directory only
  (absolute/`..` paths rejected). Notify is not integration-ready - for
  isolated modes always call `handoff --run-id` before apply. Integrate is
  **mode-aware** ([integration-modes.md](plugin/references/integration-modes.md)):
  review never auto-applies; auto may apply a dual-condition-ready patch after
  revalidation; one-shot code `integration=direct` lands source edits live; ACP
  peer `integration=direct` applies a verified ready patch only at peer-stop
  (after always-isolated worktree work) and still requires consent. The plugin
  never auto-commits, merges, cherry-picks, or pushes. **runMode direct**
  produces no handoff artifacts by design - the artifacts' value is the
  isolation evidence that runMode direct cannot attest; use runMode hardened for
  verified handoff and for ACP peer.
- **ACP peer channel (default; opt out with `GROK_DISABLE_ACP=1`):** the default
  multi-turn peer path. Peer **always** runs in an external retained worktree
  (private home + sandbox-to-worktree); prompt-time edits never live-edit the
  operator checkout. Peer-stop runs the contract's `requiredValidation` as
  **real commands** and sets `integration.ready` only from authoritative,
  non-forgeable command evidence - never a synthesized exit status or a no-op
  build gate. On ready, **peer-stop itself** integrates via the active mode and
  the **shared auto/peer apply spine** (exclusive apply lock + durable applied
  marker, best-effort patch integrity recheck under trusted local state,
  NUL-safe dirty status, numstat + header-union touch set with pure-rename both
  sides and `blocked-patch-headers` fail-closed, protected-path pre-block via
  [deny-write-globs.json](plugin/references/deny-write-globs.json)
  (`blocked-protected-path` before check/apply; tree unchanged), dirty-overlap,
  `git apply --check`, apply, reverse-on-failure - see
  [integration-modes.md](plugin/references/integration-modes.md) Shared apply
  spine): `direct` and `auto` both apply the verified ready patch (`direct`
  additionally requires per-repo direct consent); `review` / `worktree` retain
  for manual parent apply. **Not** via `/grok:handoff`, which stays code-mode
  only and still refuses peer runIds. Peer direct is therefore **stop-time
  apply**, not one-shot code live-edit direct. Peer-stop final envelope rewrite
  is rewrite-before-write/store/finalize: onStdout computes final
  emitStdout/effectiveCode before first write, then stdout write, then
  storeJobStdout, then updateJob/finalize, then notify; peer-stop is **not**
  completion-notification eligible (`NOTIFY_ELIGIBLE_MODES` excludes peer).
  Peer lifecycle honesty: durable terminal mark before restop success; mandatory
  `promptsHandled` persist; `startToken` identity fail-closed; control frame
  caps (~4 MiB); active-proc is never pid-scan-unregistered on kill refusal.
  Progress-chunk redaction is **per-frame**: a secret split across ACP
  `session/update` frames may partially land in `progress.jsonl` before a full
  token shape is visible to the scanner (residual risk; not a complete secret
  firewall). Control-socket payloads and turn envelopes are scanned with the
  same `assert_no_secret_material` path as stdout envelopes. The opt-out env
  gate is enforced in the wrapper (not companion-only). `pre_tool_use` may
  appear as an initialize capability only - the wrapper does not register a
  deny hook (NON-enforcement; OS sandbox + C6 child pins enforce). Do **not**
  claim full runtime tool-approval enforcement beyond local CLI parse +
  initialize probe evidence.
- **D4(a) exact injected denylist on direct REDACT_SCRIPT:** runMode=direct
  stdout redaction loads the same production path as hardened private-home
  registration (`AUTH_FILE_NAMES` + `register_injected_secrets_from_home` /
  `redact_injected_secrets` / `assert_no_secret_material`). Unreadable auth
  yields an empty denylist (pattern scan still runs); denylist always cleared
  in `finally`. Direct OS sandbox limits are unchanged (write confinement to
  repo root + private tmp; not a read firewall).
- **Protected content-hash including ignored protected paths + nested git metadata:**
  deny/snapshot scope fingerprints use content+mode for protected paths even
  when gitignored (same-size+mtime rewrite still flips). Sensitive git metadata
  covers root `.git`, nested workspace gitdirs (e.g. `vendor/.../.git`),
  `.git/modules/**` (including multi-component module paths such as
  `modules/libs/foo/...`), and in-workspace gitfile targets (config/HEAD/
  packed-refs, hooks/**, refs/**). Sensitivity under modules/** uses
  discovered/snapshotted `git_roots` longest-prefix + shared suffix classifier
  (module path components may be named hooks/refs/objects/logs; token peels
  are not used). Ordinary module metadata (index/objects/logs) is not
  auto-restored. Nested gitdir discovery is still bounded
  (`MAX_NESTED_GIT_DISCOVERY`) and fail-closed on overflow; hooks/refs inventory
  streams without an artificial file-count cap (real walk/read errors fail
  closed as `protected-path-write`). Watched regular git files are
  stream-hashed (SHA-256, chunked) regardless of size - never a
  `stat:size:mtime:mode` fallback for oversized or unreadable hooks (open/
  non-ENOENT lstat errors fail closed). Snapshot/restore/guard use the actual
  gitdir absolute path for gitfile roots (logical `.git/...` keys never write
  under the gitfile itself). External linked common dirs stay outside full
  inventory. Bulk ignored caches stay stat-only.
- **Patch secret denylist scan:** handoff patch generation fails closed on
  secret-shaped material and on exact injected-denylist occurrence in patch
  bytes (not pattern-only). Path inventory uses NUL-safe bytes with
  `surrogateescape` for non-UTF-8 filenames.
- **Apply lock honesty:** exclusive apply lock + durable marker is concurrent
  restop safety under trusted local state - **not** an atomic TOCTOU seal.
  Ownerless/unknown locks never age-reclaim (manual cleanup if abandoned
  without a durable owner).

### Direct-default trust posture (integration=direct)

Canonical product matrix:
[plugin/references/integration-modes.md](plugin/references/integration-modes.md).
**Do not** confuse **integration=direct** (how edits land) with **runMode=direct**
(installed CLI security posture) - both use the word "direct". Also **do not**
equate one-shot **code** direct (live tree) with **ACP peer** direct (stop-time
apply after an always-external worktree).

**integration=direct is the default** product name for **code** and **peer-stop**
landing prefs. **`/grok:implement` always forces an isolated worktree +
verify-only handoff and never lands live**, even when the workspace default is
direct/auto. Under runMode hardened, one-shot **code** direct is
**hardened-direct**: private auth home + OS sandbox write-confined to the
**repo root** (+ private tmp) + secret redaction on the stdout envelope. ACP
**peer** keeps isolation on an external worktree for the whole session and only
lands via stop-time apply when ready + (for direct) consented.

Honest limits for **one-shot code** direct (these are deliberate, not temporary
gaps to paper over):

1. **Worktree isolation is gone in code direct.** Source edits land in the
   operator checkout live. There is no pre-apply dual-condition gate and no
   forensic patch required for the edit to exist. (ACP peer always keeps the
   external worktree during prompts.)
2. **Private home does not protect the tree.** It isolates Grok credentials /
   config for the child, not your working tree, `.env`, or git objects.
3. **Sandbox confine ≠ protect `.git` / `.env` inside the repo.** The write
   grant is whole-root. A deny-list scan plus **post-run rollback** covers
   `.env` / `.env.*`, key files (`*.pem` / `*.key` / `*.p12` / `*.p8`, SSH keys,
   `.netrc` / `.npmrc` / `.envrc`), and sensitive git metadata for every
   **in-workspace** gitdir: root `.git`, nested workspace gitdirs
   (e.g. `vendor/.../.git`), `.git/modules/**`, and in-workspace gitfile
   targets (`gitdir:` under the repo). Logical keys (`.git/HEAD`, hooks, refs)
   restore to the **actual** absolute gitdir, never under a gitfile path.
   Sensitive set: `config` / `HEAD` / `packed-refs`, `hooks/**`, `refs/**`
   after any multi-component `.git/modules/**` path (moved branch or planted
   hook/ref is reverted or removed). Nested gitdir discovery is bounded and
   fail-closed on overflow; hooks/refs walks stream fully (no artificial file
   count). Watched regular files stream-hash in bounded memory (including
   multi-MiB hooks); unreadable watched paths fail closed rather than using a
   forgeable stat signature. Snapshot persists a `git_roots`
   prefix->actual-gitdir map used by restore even if a gitfile pointer is
   rewritten after the run. After-guard **unions** baseline roots with live
   discovery so new in-workspace redirect targets and plants still surface as
   protected-path-write; restore prefers the baseline map and clears live-only
   extras for the same logical key when possible. `modules/**` is inventoried
   under every discovered abs gitdir (root/nested free-standing and gitfile
   targets). Real submodule aliases (`vendor/lib/.git` gitfile ->
   `.git/modules/lib`) retain **both** logical prefixes even when they share
   one abs gitdir. Restore order is `abs_paths` exact, then baseline
   `git_roots` children, then live-only; the bare gitfile marker key never
   maps onto its target dir. Marker bytes are stored under a dedicated
   snapshot store prefix and restored as a file when snapshotted. **External**
   linked-worktree common dirs (gitfile target outside the workspace) are
   **not** fully inventoried; only the git-resolved primary HEAD/config/hooks/refs
   fingerprint remains for that case. `.git/index` and `.git/COMMIT_EDITMSG`
   are **not guarded** (benign working state git rewrites on ordinary reads
   like `git status`); loose `.git/objects` are **not tracked**
   (content-addressed and inert until a watched ref points at them). This is
   best-effort detection + rollback, not seatbelt subpath prevention.
4. **Grok can still READ your files** (documented D-SECRETREAD). Write
   confinement is not a read firewall.
5. **Consent gate:** first direct landing without `setup --integration direct`
   (per target repo) fails closed with a trust summary - for code direct runs
   and for peer-stop direct apply. Env / userConfig alone never counts as
   consent.

Choose **integration=auto** or **review** when you want isolation and a verified
patch before anything lands on the operator tree (one-shot code), or when you
want peer-stop to retain rather than apply. Prefer those for untrusted or
high-risk changes. Note ACP peer already isolates prompt-time work either way.

### Session archives

Each live Grok spawn may leave a session store under the private home at
`~/.grok/sessions` (cwd-bucketed session dirs, per-bucket `prompt_history.jsonl`,
and root `session_search.sqlite`). Before private-home destroy, the wrapper
copies that whole tree into the run directory at
`runs/<runId>/session/sessions/` (dirs `0700`, files `0600`) and writes
`session/session-meta.json` with the argv session id. The archive holds
**operator prompt / conversation content** (task text and related model state).
It is run-dir private state only - never emitted on stdout - so envelope secret
redaction does not apply to the on-disk archive. Treat the state root as
sensitive. `cleanup --run-id --confirm` removes the entire run directory
(including `session/`), so archived session data is deleted with the run.

Full design notes: [docs/OPEN-SECURITY-DECISIONS.md](docs/OPEN-SECURITY-DECISIONS.md).

## Supported platforms

- **macOS** with Seatbelt: live modes supported when a working Grok CLI is installed
  and logged in (any build; platform probe gate still applies).
- **Linux / Windows**: live modes fail closed with `probe-required` until a sandbox profile
  is verified for that platform.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive reports.

1. Prefer a private channel to the maintainer listed in
   [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) (author name /
   repository security advisories when the public repo is published).
2. Include: affected version or commit, reproduction steps, impact, and whether secrets
   or host escape are involved.
3. Allow reasonable time for a fix before public disclosure.

## Scope

In scope: the wrapper (`plugin/wrapper/`), the Claude Code plugin (`plugin/`), and docs that
describe security behavior.

Out of scope: vulnerabilities in the Grok CLI itself, xAI services, or third-party
projects you point the tool at.
