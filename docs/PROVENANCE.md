<!-- docs/PROVENANCE.md -->

# Provenance: how this was built, and why

This document captures the design decisions and the hardening record for the Grok CLI
companion. It is the "how and why" behind the code. The "what" lives in the code, the
SKILL.md, the command markdown, and `references/README.md`.

## What this is

Two layers:

- **The wrapper** (`plugin/wrapper/`): a stdlib-only Python engine that runs the
  Grok CLI with per-run private auth-home isolation, a verified OS sandbox, external
  worktree confinement, complete rule injection, a fail-closed secret scanner, one
  machine-readable JSON result envelope on stdout, and a JSONL progress stream. It is the
  sole owner of all safety behavior. It is harness-agnostic: any agent, or a human, can
  shell out to it.
- **The plugin** (`plugin/`): a thin Claude Code surface over that engine -
  `/grok:{preflight,review,reason,code,verify,status,cleanup,setup}` commands, the
  `grok-rescue` subagent, an opt-in Stop-review gate, and a live streaming relay. It adds
  no safety logic; every command shells to the wrapper and relays its envelope verbatim.

## Design decisions (the D-log)

- **D-NET (network permitted):** the wrapper allows Grok network egress. Grok is an
  internet-connected model; egress is intended, not a leak.
- **D-WEB (web search):** Grok web/X search is available via `--web`. Wave 1 makes it
  default-on for adversarial-review and reason (see D-W1-GROUND).
- **D-SECRETREAD (accepted secret-read gap):** on the probed Grok CLI (0.2.101) the OS
  sandbox confines WRITES but cannot deny arbitrary READS. So a sandboxed run can read a
  secret file; it just cannot exfiltrate by writing outside the workspace, and the secret
  scanner blocks secret-shaped material from reaching stdout. This gap is accepted and
  surfaced as a preflight advisory (`secretReadDenial=false`), not hidden.
- **D-PORT (portability):** all platform-specific behavior goes through one abstraction
  layer so the tool is cross-platform and fails closed on any platform whose sandbox
  cannot be enforced. macOS is the live-probed platform; others fail closed until probed.
- **D-STREAM (streaming without direct-ACP):** `--output-format streaming-json` on the
  single-shot, OS-sandboxed path streams live thoughts and tool calls for ALL modes. This
  removed the need for a direct Agent-Client-Protocol transport, which would have run
  tools in-process with NO OS sandbox. Streaming rich progress AND keeping the OS sandbox
  are not in tension: the sandboxed single-shot path gives both.
- **Last-validated CLI stamp (advisory):** Grok ships often. `accepted-version.json`
  records the last maintainer-probed build for evidence/docs. Runtime accepts any
  working `grok --version` so users are not blocked by normal CLI updates.
- **Single envelope, wrapper is sole author:** the wrapper emits exactly one JSON result
  envelope on stdout. The plugin never fabricates one. A pre-execution failure (for
  example the wrapper binary not found) is a stderr + non-zero exit, not a fake success
  envelope.
- **Fail closed, everywhere:** unknown platform, unenforceable sandbox, malformed stream,
  missing/relative state root, secret-shaped output, a Stop-gate throw - all resolve to a
  safe refusal, never a silent pass.
- **Secret redaction policy:** secret-shaped material is redacted from everything that
  reaches stdout (the envelope) while raw text stays only inside the private 0700 run dir
  (progress.jsonl). `status` redacts the embedded copy on readback. The scanner remains a
  strict final fail-closed backstop after redaction.
- **D-W1-GROUND (Wave 1 grounding default):** grounded-where-it-matters - live search
  default-on for adversarial-review and reason, opt-in for review and code.

## Hardening record: the dual-lens review loop

The tool was hardened with a deliberate methodology worth reusing: **two independent
adversarial lenses, run every round, iterating until BOTH come up empty.**

1. **Our lens - adversarial multi-agent review.** Cold-context agents read whole files
   (never the diff, never our intent), each finding surfaced then adversarially verified
   by independent skeptics whose job is to REFUTE it; a finding survives only if a
   majority cannot refute it.
2. **Grok's lens - dogfood.** The wrapper reviews its own engine (`/grok:review` on the
   wrapper code). An independent model, reading cold, catches what an agent primed on the
   changed surface misses.

Why both: the two lenses fail differently. Our agents scoped tightly to the changed
surface; Grok read the whole wrapper cold and found pre-existing bugs. A single lens
would have shipped them.

### What the rounds found (all confirmed findings fixed with tests)

- **Round 1 (ours):** 17 findings. Baseline hardening.
- **Round 2 (ours):** 11 findings, including a secret-leak REGRESSION that round 1's own
  fix introduced (the redactor stripped the "bearer" label but left the token, and passed
  the scanner because of it). Fixed with the test round 1 lacked: assert the secret VALUE
  is absent from redacted output.
- **Grok dogfood round 1:** 9 findings, several real and NEW because Grok read cold and
  whole - auth-home credential residue (private homes never wrote an owner marker, so the
  reaper could never reap them), a raw `startswith` model-family check accepting the wrong
  family, missing secret shapes (AWS/GitHub/Slack/PEM), a too-narrow verify artifact
  allowlist, worktree orphan on marker-write failure, `load_run_record` accepting a
  non-object, a schema walker ignoring `enum`.
- **Round 3 (ours):** 9 findings, including another secret-scanner gap (a tuple bypassed
  BOTH the scanner and the redactor because the recursion was duplicated dict/list/str
  only - fixed by one shared tree-walker), and a process-kill REGRESSION that round 2's
  gate-kill fix introduced (it could signal process group 0, the caller's own group).
- **Grok dogfood round 2:** 10 findings, including a REGRESSION from round 3's verify
  artifact fix (over-broadened to exempt any path component named `dist`/`build`, so a
  write under `src/dist/` slipped past the change gate), plus depth: the auth-residue
  reaping window (audit only in preflight with a 24h gate), model output not redacted (a
  review quoting a real secret would hard-fail and lose its body), the sandbox writable
  root spanning all of the OS temp dir, failed auth teardown still exiting 0, and
  progress.jsonl not being 0600.

The recurring lesson, visible three times: **a fix wave can introduce a new bug.** That is
exactly why the loop repeats until a round is clean from both lenses, rather than stopping
after the first green fix.

### Classes of defect the loop hardened

- Secret handling: scanner container coverage (tuples), secret shapes (bearer incl
  all-letter, sk-/xai-, AWS AKIA, GitHub gh*_, Slack xox*-, PEM), redaction removing the
  VALUE not just a label, redaction of stderr and model output, private-file permissions.
- Run lifecycle: terminalizing `run.json` under the REAL run id on ANY escaped exception;
  degrading (not crashing) when a progress write fails.
- Process control: never signal process group 0; reach the innermost Grok CLI on kill;
  Windows tree-kill; timeout that cannot hang.
- Auth / worktree / sandbox: owner markers + residue reaping, worktree/branch orphan
  cleanup, sandbox writable-root scope, non-self-extending sandbox profiles.
- Stop-gate: fail-closed on any throw, recursion guard, buffer and timeout bounds.

## Current status

The dual-lens loop is the ongoing quality bar for substantive changes: prefer shipping
only after a round produces zero confirmed findings from both independent adversarial
review and Grok dogfood, then merge with explicit human approval. See `docs/roadmap.md`
for Waves 1-3 (live adversary, debate, autonomy) and Codex support.
