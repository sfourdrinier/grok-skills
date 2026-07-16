<!-- docs/OPEN-SECURITY-DECISIONS.md -->

# Security decisions (resolved)

The dual-lens review loop (adversarial agents + Grok dogfood, 6 rounds) fixed every
mechanical bug it found. Four remaining findings were design choices about the wrapper's
security posture rather than mechanical defects. They are documented here for operators
and auditors; the resolutions below are what shipped.

**Fail-closed carve-out (v1.2.8):** Grok CLI **build-string mismatch** vs
`accepted-version.json` is **not** fail-closed. Any working `grok --version` is
accepted. Fail-closed still applies to unverified platform/sandbox/stream and to
unusable `--version` output.

## Resolutions (2026-07-15)

All four decisions are now resolved. The original analysis is preserved below unchanged for
provenance; this section records the decision taken and exactly what shipped.

- **D1 - build gate runs Grok-modifiable scripts unsandboxed: RESOLVED via (b)
  refuse-if-gate-scripts-changed, plus the existing post-build-gate escape detection.**
  In `code` mode the wrapper captures the target `package.json`'s pristine base-commit gate
  script definitions (`build`/`test`/`typecheck`/`lint`) BEFORE Grok runs, then compares the
  gate scripts it would execute against that pristine state. If any was ADDED or CHANGED by
  the run (or the base manifest cannot be read), the gate is NOT executed: it is skipped
  fail-closed and the envelope records `gate-scripts-modified` (a warning), so a
  Grok-rewritten build/test/typecheck/lint script never runs in the operator environment.
  The code result (worktree changes) still returns; only gate EXECUTION is refused. This
  complements, and does not replace, the existing post-build-gate original-checkout escape
  re-scan. Option (a) (fully sandboxing the gate) remains the long-term ideal and is left as
  future work.

- **D2 - exfil composition (network egress + absolute-path secret reads): ACCEPTED as the
  trusted-input model, documented.** `code`/`verify` remain a hardened runner for input you
  trust (your own repos), not a sandbox against an adversarial model. An opt-in `--no-net`
  flag for untrusted repos is noted as a future option and is NOT built now. The limit is
  documented here and in the plugin README.

- **D3 - sandbox write-confinement is verified, not wrapper-enforced: ACCEPTED as a
  Grok-imposed limitation, documented.** The OS sandbox is the enforcement layer and the
  wrapper VERIFIES it (including Grok's mandatory shared session-temp roots) rather than
  authoring a narrow profile. The clearly-fail-closed parts (empty/expected-grant-missing
  `read_write_paths`) already fail verification. A wrapper-authored narrow `sandbox-exec`
  profile is future work, NOT built now.

- **D4 - auth.json is tool-readable; redaction was pattern-only: RESOLVED via (a) exact-value
  injected-credential redaction.** At private-home creation the wrapper reads the COPIED
  `auth.json` and captures its string leaf values (length >= 16, key-name independent) into a
  per-run denylist, then masks any EXACT occurrence of those values anywhere in the stdout
  envelope (response text, structured, warnings, error detail; both string values and dict
  keys) with `[redacted-injected-value]` BEFORE emission. This runs IN ADDITION to the
  existing pattern scanner, which is preserved unchanged as the fail-closed last line of
  defense. Extraction is fail-safe: a malformed/missing `auth.json` degrades to an empty
  denylist without crashing the run.

## TL;DR - the honest security model

The wrapper's real, enforced guarantees are:

- **Write confinement:** the sandboxed Grok child cannot write outside the run's workspace
  (verified via sandbox enforcement checks).
- **Private auth home:** each run gets an isolated `HOME` with a 0600 copy of `auth.json`,
  torn down after the run (now fail-closed on teardown failure, with active-home reaping
  protection).
- **Single redacted stdout envelope:** the one JSON result on stdout is scanned and
  redacted for known secret shapes before emission; raw text stays only in the 0700 run
  dir.
- **Fail-closed everywhere:** unknown platform, unenforceable sandbox, malformed output,
  escaped exception, signal - all resolve to a safe refusal.

What the wrapper is NOT: **a complete sandbox against an adversarial Grok.** It is a
hardened runner for *trusted input* - code you are willing to let Grok read and (in `code`
mode) run a build over. The four items below are where that boundary lives.

## The pattern-redaction limitation (proven by the review loop)

The stdout secret scanner/redactor is **pattern-based** (known shapes: bearer, JWT,
`sk-`/`xai-`/`sk-proj-`/`sk-ant-`, AWS `AKIA`/`ASIA`, GitHub `ghp_`/`github_pat_`, Slack
`xox*`, PEM). Across six rounds the review loop repeatedly found new shapes the patterns
missed (the `sk-` pattern alone oscillated over-match -> under-match -> fixed). This is
inherent: **regex redaction can catch known secret shapes as defense-in-depth, but it can
never be a complete secret firewall.** Chasing every shape does not converge. We fixed the
concrete known cases and stop there deliberately.

The limitation has three facets, all surfaced and best-effort-mitigated by the loop, none
fully closable by patterns:

- **Value shapes** - the set of known prefixes/formats (above). New providers appear; the
  set is never complete.
- **Key names** - the heuristic that flags a secret-shaped dict key (`...token`,
  `...secret`) misses novel casings (now handles acronym-glued forms like `IDTokenHint`).
- **Streaming timing** - a secret split across two live-relay poll ticks can have its first
  half printed before the mask applies, and a terminal cannot be un-printed. Mitigated with
  tail-holdback (hold the trailing partial line until the next tick), which closes the
  common case; a secret split across a hold-back boundary with no further events is a
  documented residual.

The single robust improvement that does NOT depend on patterns is D4(a): exact-value
redaction of the one secret the wrapper itself injects (`auth.json`).

**Recommended framing for the README:** "The redactor masks common secret shapes on the
result surface as defense-in-depth. It is not a guarantee. The real controls are
write-confinement, auth-home isolation, and the trusted-input assumption. Do not point
`code`/`verify` at a workspace whose secret exposure to Grok you have not accepted."

---

## Decision D1 - the build gate runs Grok-modifiable scripts unsandboxed (Grok r3 #2, critical)

In `code` mode, after Grok finishes, the wrapper runs the project's package-manager
`build`/`test`/`typecheck` scripts **in the wrapper process, outside the sandbox and
private HOME**. Grok can rewrite those scripts in the worktree, so this is a host-side
code-execution channel for whatever Grok wrote.

Options:
- **(a) Sandbox the gate** - run the build gate under the same OS sandbox + private HOME as
  the Grok child. Most principled; risk: real builds often need network (dep install) and
  broad writes (dist/cache), so this may break legitimate gates.
- **(b) Refuse if gate scripts changed** - diff `package.json` `scripts.build/test/
  typecheck` against the base; if Grok modified them, do not run the gate (fail closed with
  a clear reason). Simple and fail-closed; risk: false positives when a legitimate task
  intentionally changes a build script.
- **(c) Accept + document** - keep running the gate, document that `code` mode executes the
  workspace's (Grok-modifiable) build scripts and must only be pointed at trusted repos.

**Recommendation:** (b) as the default fail-closed guard, with (c)'s documentation. (a) is
the long-term ideal but needs its own design pass.

## Decision D2 - exfil composition (Grok r3 #8, accepted-risk today)

`code`/`verify` allow `run_terminal_command`; network egress is permitted (D-NET);
absolute-path reads of host secrets are not denied (D-SECRETREAD). Composed, Grok can read
`~/.ssh`/`~/.grok/auth.json` by absolute path and exfiltrate over the network.

Options:
- **(a) Restrict network** in `code`/`verify` (deny egress or allowlist) - closes the
  exfil channel; risk: breaks tasks that legitimately need the network (dep install, API
  calls) and web-grounded reasoning.
- **(b) Accept + document** as the trusted-input model (current posture).
- **(c) OS-level read confinement** (future) - a wrapper-authored `sandbox-exec` profile
  that denies reads outside the workspace; noted as future hardening in the Phase 1 spec.

**Recommendation:** (b) for launch with prominent documentation, (c) tracked as the real
fix. Consider (a) as an opt-in `--no-net` flag for untrusted repos.

## Decision D3 - sandbox write-confinement is verified, not wrapper-enforced (Grok r4 #4, r5 #2/#6, important)

`sandbox.verify_enforcement` adds `mandatory_session_temp_roots()` (on macOS, `/tmp` and
`/private/tmp` wholesale) to the expected writable roots, so a child writing under `/tmp`
still passes verification even though the policy root was narrowed to `<home>/tmp`. This is
driven by Grok's own mandatory session-temp usage.

Rounds 5-7 showed this is a boundary, not a single bug: the wrapper's `writable_roots` are
a *check on Grok's own telemetry* against a broad allowlist, not an enforcement input that
constrains Grok. We tightened the clearly-fail-closed part (an empty or expected-grant-
missing `read_write_paths` now fails verification - Grok r5 #3), but the fundamental choice
remains: the OS sandbox is the enforcement layer, and the wrapper *verifies* it rather than
*authoring* a narrow profile.

Options:
- **(a) Narrow if possible** - verify whether Grok can be constrained to `<home>/tmp` only;
  if so, drop the wholesale `/tmp` from the expected roots.
- **(b) Accept as a Grok-imposed limitation** and document that write-confinement includes
  the shared session-temp dir.

**Recommendation:** attempt (a); if Grok requires the shared temp, (b) with documentation.
(This one is closest to mechanically fixable - it just needs a live check of whether Grok
tolerates the narrower temp.)

## Decision D4 - auth.json is tool-readable; redaction is pattern-only (Grok r4 #5, important)

The child's `HOME` holds `auth.json` (Grok needs it to authenticate), and review/code/verify
tools include `read_file`. So Grok can read its own credential, and the pattern redactor may
not catch it (a JSON credential is not a bearer-prefixed string).

Options:
- **(a) Exact-value redaction of the injected secret (recommended)** - at setup the wrapper
  KNOWS the exact `auth.json` token value it injected; add that exact value to a
  per-run redaction denylist so it is masked on stdout by exact match, regardless of shape.
  This robustly closes the single most important case (the credential the wrapper itself
  injected) without relying on patterns. Additive, safe, does not change the trust model.
- **(b) Accept** pattern-only redaction as defense-in-depth (current).

**Recommendation:** implement (a) - it is a concrete, bounded, high-value hardening and the
one place we control the secret's exact value.

---

## The overarching recommendation

Position this as a **trusted-input developer tool**, document the model and its limits
prominently (the framing above), implement **D4(a)** now (cheap, robust), default **D1(b)**
(fail-closed gate-script guard), ship **D2(b)** with an opt-in `--no-net`, and resolve
**D3** with a quick live check. That gives an honest, well-documented security posture that
people can trust, without pretending the redactor is a firewall.

None of this blocks the plugin's usefulness for its intended use (a hardened Grok companion
for your own repos); it just makes the boundary explicit before it goes public.
