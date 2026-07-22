<!-- plugin/wrapper/scripts/tests/fixtures/probe-report-linux.md -->

# Linux Landlock grounding probe report (2.0.1)

Live, authenticated probes of the installed Grok CLI on Linux, run on
2026-07-22 (UTC). Isolation matched the macOS Task 0 discipline: private
`HOME` (temp dir with only `auth.json` copied at mode 0600), unique
`--leader-socket` per run, disposable `git init` cwd under the OS temp dir.
No authentication file contents were read, printed, hashed, or stored.

This report is the committed evidence that unblocks
`PROBED_PLATFORMS` for `linux` and pins
`expected_sandbox_platform() == "linux/landlock"`.

---

## Host and binary

| Item | Value |
| --- | --- |
| Kernel | Linux 6.8.x (`CONFIG_SECURITY_LANDLOCK=y`, Landlock in LSM list) |
| Arch | x86_64 |
| Grok binary | `~/.grok/downloads/grok-0.2.110-linux-x86_64` (ELF pie) |
| Version line | `grok 0.2.110 (b70e0acd09) [alpha]` |
| bubblewrap | `bwrap` 0.9.x on PATH (required for deny-glob paths) |
| Auth | `~/.grok/auth.json` present (0600); copied into private homes only |

Placeholder convention below: `<operator-home>`, `<private-home>`, `<cwd-repo>`.

---

## Pinned constants (Linux)

| Constant | Pinned value | Evidence |
| --- | --- | --- |
| `PROBED_PLATFORMS` includes | `linux` | This report |
| `ProfileApplied.platform` | `linux/landlock` | All ProfileApplied events below |
| Session-temp write roots (typical) | `<cwd>`, `<private-home>/.grok`, `/tmp`, `/var/tmp` | workspace ProfileApplied `read_write_paths` |
| `mandatory_session_temp_roots` (wrapper) | `/tmp`, `/var/tmp` (+ `XDG_RUNTIME_DIR` when set) | Matches telemetry; already in platformsupport |
| Secret-read denial | **not proven** (D-SECRETREAD holds on Linux) | Built-in + custom deny_read_globs still readable |
| Network | Child-process block possible via seccomp on Linux; not product-gated | CLI docs; workspace often `restrict_network: false` |

Built-in sandbox profile names and headless `permission-mode auto` match the
macOS Task 0 pins (`read-only` / `workspace` / custom `grok-skills-<mode>`
extends). No new mode matrix.

---

## Step A: ProfileApplied telemetry (workspace)

Invocation shape (every model-calling probe):

```text
HOME=<private-home>
grok --prompt-file <prompt> --verbatim --cwd <cwd-repo>
     --model grok-4.5 --output-format json --permission-mode auto
     --tools ... --no-subagents --no-memory --disable-web-search --no-plan
     --sandbox <profile> --max-turns N
     --leader-socket <private-home>/.grok/leader.sock
```

Simple PONG task under `--sandbox workspace` (no tools):

```json
{
  "event_type": "ProfileApplied",
  "profile": "workspace",
  "workspace": "<cwd-repo>",
  "platform": "linux/landlock",
  "enforced": true,
  "restrict_network": false,
  "read_write_paths": ["<cwd-repo>", "<private-home>/.grok", "/tmp", "/var/tmp"]
}
```

`enforced: true` and `platform: linux/landlock` are the verify_enforcement
accept criteria for Linux.

---

## Step B: Write confinement

**(B1) Write inside workspace.** Task: create `probe.txt` with content `ok`
under `--sandbox workspace` with write tools allowlisted.

- Result: `probe.txt` created inside `<cwd-repo>` with content `ok`.
- ProfileApplied: `platform=linux/landlock`, `enforced=true`.

**(B2) Write outside workspace (real-home escape dir).** Task: create
`<operator-home>/gs-escape-XXXX/escape.txt` via write or shell tools.

- Result: file **not** created; run stopped (`Cancelled`).
- Telemetry: `FsViolation` with `operation: write` and `target` the absolute
  escape path under the operator home (outside `read_write_paths`).
- Conclusion: `workspace` confines writes to cwd + private Grok state +
  session temp roots, same security boundary the macOS probe established.

---

## Step C: Custom profile (extends workspace)

Private-home `sandbox.toml`:

```toml
[profiles.grok-skills-code]
extends = "workspace"
deny_read_globs = [
  "<operator-home>/.ssh",
  "<operator-home>/.ssh/**",
]
```

`--sandbox grok-skills-code`, PONG task:

```json
{
  "event_type": "ProfileApplied",
  "profile": "grok-skills-code",
  "platform": "linux/landlock",
  "enforced": true,
  "read_write_paths": ["<cwd-repo>", "<private-home>/.grok", "/tmp", "/var/tmp"]
}
```

Custom `grok-skills-*` naming (wrapper policy) applies without shadowing
built-ins. Wrapper `render_sandbox_toml` remains valid on Linux.

---

## Step D: Secret-read denial (still open)

Planted non-secret sentinels (`sentinel-not-a-secret`) under a disposable
directory directly under the real home (outside session temp / workspace):

- `<secret-base>/.ssh/probe_sentinel`
- `<secret-base>/external-secret/secret.txt`

Task: read both with `read_file` and print contents.

| Profile | Sentinel contents in model output? |
| --- | --- |
| `read-only` | **Yes** (readable) |
| `workspace` | **Yes** |
| Custom extends `read-only` + `deny_read_globs` for both trees | **Yes** (still readable) |

No built-in or custom deny_read_globs path denied credential-shaped **reads**
on this Grok Linux build. Same accepted residual as macOS Seatbelt
(**D-SECRETREAD**): write confinement is the fail-closed boundary; secret-read
denial stays advisory `false`.

---

## Step E: Preflight gate before this pin

Before adding `linux` to `PROBED_PLATFORMS`, hardened companion preflight on
this host returned:

```json
{
  "status": "failure",
  "error": {
    "class": "probe-required",
    "detail": { "platform": "linux", "probedPlatforms": ["macos"] }
  }
}
```

Auth and version checks already succeeded. After this report + pin, live modes
are expected to proceed past the platform gate when `bwrap` is on PATH.

---

## Prerequisites for operators (Linux)

1. **Kernel:** Landlock-capable (5.13+, LSM includes landlock).
2. **bubblewrap:** `bwrap` on PATH (`apt install bubblewrap` / equivalent).
3. **Grok CLI:** Linux binary installed and logged in (`grok --version` works).
4. **Python 3 + Node** on PATH (wrapper / companion).

The wrapper fails closed with `probe-required` if the platform is unprobed or
if Linux is missing `bwrap`.

---

## What this does not claim

- Not a hermetic network sandbox for in-process tools (LLM / web).
- Not secret-read denial.
- Not universal "all Linux distros": evidence is one Ubuntu-class x86_64 host
  with Landlock + bwrap. aarch64 and unusual LSM configs need their own probes
  if operators hit `sandbox-failure` / `probe-required`.
- Windows remains unprobed.

---

## Cross-links

- macOS Task 0 report: `probe-report.md`
- Secret-read custom probe (macOS; gap accepted): `sandbox-custom-probe.md`
- Platform gate: `groklib/platformsupport.py` (`PROBED_PLATFORMS`,
  `expected_sandbox_platform`, `require_probed_platform_for_live` (single SSOT
  gate including Linux bwrap))
- Enforcement verify: `groklib/sandbox.py` (`verify_enforcement`)
