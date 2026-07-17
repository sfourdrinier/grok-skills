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
- **Worktree isolation** for `code` when **integration** is `auto` / `review` /
  `worktree` (external worktree + escape detection). **Not** claimed for
  **integration=direct** (default) - see [direct-default trust posture](#direct-default-trust-posture-integrationdirect) below.
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
  revalidation; integration=direct lands source edits live. The plugin never
  auto-commits, merges, cherry-picks, or pushes. **runMode direct** produces no
  handoff artifacts by design - the artifacts' value is the isolation evidence
  that runMode direct cannot attest; use runMode hardened for verified handoff.
- **ACP peer channel (default; opt out with `GROK_DISABLE_ACP=1`):** the default
  multi-turn peer path. Peer-stop runs the contract's `requiredValidation` as
  **real commands** and sets `integration.ready` only from authoritative,
  non-forgeable command evidence - never a synthesized exit status or a no-op
  build gate. Integration is applied by **peer-stop itself** per the active
  integration mode (review retains the worktree patch; auto/direct apply after
  `git apply --check`, reversing on failure), **not** via `/grok:handoff`, which
  stays code-mode only and still refuses peer runIds. Progress-chunk redaction is
  **per-frame**: a secret split across ACP `session/update` frames may partially
  land in `progress.jsonl` before a full token shape is visible to the scanner
  (residual risk; not a complete secret firewall). Control-socket payloads and
  turn envelopes are scanned with the same `assert_no_secret_material` path as
  stdout envelopes. The opt-out env gate is enforced in the wrapper (not
  companion-only).

### Direct-default trust posture (integration=direct)

Canonical product matrix:
[plugin/references/integration-modes.md](plugin/references/integration-modes.md).
**Do not** confuse **integration=direct** (how edits land) with **runMode=direct**
(installed CLI security posture) - both use the word "direct".

**integration=direct is the default** for code / implement / peer. Under runMode
hardened it is **hardened-direct**: private auth home + OS sandbox write-confined
to the **repo root** (+ private tmp) + secret redaction on the stdout envelope.

Honest limits (these are deliberate, not temporary gaps to paper over):

1. **Worktree isolation is gone in direct.** Source edits land in the operator
   checkout live. There is no pre-apply dual-condition gate and no forensic
   patch required for the edit to exist.
2. **Private home does not protect the tree.** It isolates Grok credentials /
   config for the child, not your working tree, `.env`, or git objects.
3. **Sandbox confine ≠ protect `.git` / `.env` inside the repo.** The write
   grant is whole-root. A deny-list scan plus **post-run rollback** covers
   `.env` / `.env.*`, key files (`*.pem` / `*.key` / `*.p12` / `*.p8`, SSH keys,
   `.netrc` / `.npmrc` / `.envrc`), `.git/config`, `.git/HEAD`,
   `.git/packed-refs`, `.git/hooks/*`, and `.git/refs/**` (a moved branch or a
   created ref is reverted or removed). `.git/index` is **detected but not
   restored** (git rebuilds it); loose `.git/objects` are **not tracked**
   (content-addressed and inert until a watched ref points at them). This is
   best-effort detection + rollback, not seatbelt subpath prevention.
4. **Grok can still READ your files** (documented D-SECRETREAD). Write
   confinement is not a read firewall.
5. **Consent gate:** first direct run without `setup --integration direct`
   (per target repo) fails closed with a trust summary. Env / userConfig alone
   never counts as consent.

Choose **integration=auto** or **review** when you want isolation and a verified
patch before anything lands on the operator tree. Prefer those for untrusted or
high-risk changes.

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
