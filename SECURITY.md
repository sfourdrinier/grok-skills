# Security Policy

## What this project is (trust model)

`grok-skills` is a **hardened runner for trusted input** — code and repositories you are
willing to let the Grok CLI read, and (in `code` / `verify`) exercise build/test scripts
over. It is **not** a complete sandbox against an adversarial model.

### Enforced guarantees

- **Write confinement** for the sandboxed Grok child (verified OS sandbox profile).
- **Private auth home** per run (isolated `HOME`, 0600 credential copy, teardown).
- **Exactly one machine-readable result envelope** on stdout, scanned/redacted for
  known secret shapes and for exact injected credential values.
- **Fail-closed defaults** when the platform, sandbox, or stream cannot be
  verified (not when Grok CLI build string differs from last-validated stamp).
- **Worktree isolation** for `code` (external worktree + escape detection).
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
  (`runId`, `mode`, `lifecycle`, `durationSeconds`) — do not put secrets in the
  webhook URL. At-most-once **attempt** only (no delivery guarantee; no auto-retry).
  Native OS notify expects a desktop session; prefer **webhook** for headless
  hosts until PR5 docs/setup polish. Direct mode has no push notify in 1.5.0
  (job still tracked; PR5 job-scoped marker). Isolation dirty patches may briefly
  exist as `*.diff` under the state root and are cleaned on success/cleanup
  paths; treat state root as sensitive.

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
