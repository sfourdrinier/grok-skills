<!-- docs/specs/2026-07-14-wave1-live-adversary-design.md -->

# Grok Plugin - Wave 1: The Live Adversary (Design Spec)

**Status:** Historical design (2026-07-14). **Not normative after v1.2.x.**
Current contracts: `web_defaults.py`, `grokcli_version.py`, README, CHANGELOG.

**Original status:** Approved (brainstorm 2026-07-14). Ready for implementation planning.

**Goal:** Make the Grok plugin visibly better than a generic AI code-review plugin by
leaning into what only Grok 4.5 can do: challenge your work AND ground every claim in
what is true on the internet *today*. Wave 1 delivers a live-knowledge adversarial
reviewer plus the zero-config polish that makes it feel effortless.

**Unifying concept:** *Grok as your live adversary - a second mind that knows today's
internet and is built to disagree with you.* Every Wave 1 capability is an expression of
that: live-knowledge = it knows what is current; adversarial-review = it disagrees on
purpose; zero-config UX = it feels effortless while doing it.

**Branching:** implement on a feature branch (suggested name
`wave1-live-adversary`). Phase 1+2 contracts are frozen; Wave 1 never edits them except
by strictly additive extension (a new envelope field, new wrapper flags, one new mode,
one new command).

---

## 1. Context and what already exists

Phase 1 (the wrapper, `plugin/wrapper/`) and Phase 2 (the plugin,
`plugin/`) already deliver:

- A hardened, stdlib-only Python wrapper (`grok_agent.py`) with 7 subcommands
  (`preflight`, `status`, `cleanup`, `review`, `reason`, `code`, `verify`), per-run
  private auth-home isolation, verified OS sandbox, external-worktree confinement, a
  single machine-readable C4 result envelope on stdout, and a C3 JSONL progress stream.
- Live streaming via `--output-format streaming-json` relayed into `progress.jsonl`
  (T2-0), with a Node stdlib progress relay that surfaces Grok's live thoughts/tools to
  stderr (T2-2).
- The `/grok:*` slash commands (`grok-companion.mjs`), the `grok-rescue` subagent, and an
  opt-in Stop-review gate.
- An existing, already-approved network/web capability: the wrapper supports Grok web
  search (decision D-WEB in the Phase 1 design; `--web` surface). Wave 1 builds directly
  on this - it does NOT introduce network egress for the first time.

Wave 1 adds three things on top, all additive.

---

## 2. Wave 1 scope (three components)

### Component A - `/grok:adversarial-review`

A new read-only review mode whose job is to attack, not assess.

- **Target:** identical to `/grok:review` - a repo workspace, plus an optional diff or
  task framing. Read-only: the wrapper's sandbox forbids writes on this path exactly as
  for `review`.
- **Prompt stance (the difference from `review`):** instruct Grok to assume the code or
  design under test is wrong and to build the strongest, most concrete case that it will
  break, is unsafe, violates the repo rules, or will not survive production. For each
  attack: steelman the failure mode, give a concrete reproduction or evidence, and assign
  a severity. A brief one-line fix hint per finding is allowed; a full fix is not (that
  would make it an ordinary review, not an adversary). Prompts are written as positive
  MUST/DO instructions per repo prompt-framing guidance, not DON'T walls.
- **Grounding:** live web/X search is ON by default for this mode (see Component B). An
  adversary that does not know this week's CVEs and the current API signatures is not
  credible.
- **Envelope:** reuses the `review` response shape, plus the new `citations` field
  (Component B). Findings are returned as structured, severity-ranked items so the
  command can render them grouped by severity.
- **Wrapper surface:** a new `adversarial-review` subcommand (or `review --adversarial`;
  the plan picks one after checking which keeps `grokcli`/mode wiring DRY - default
  recommendation: a distinct `adversarial_review` mode module that shares the review
  target-resolution and envelope helpers, so there is one source of truth for
  target/worktree handling and only the prompt + grounding default differ).
- **Skill surface:** `plugin/skills/adversarial-review/SKILL.md`, wired through
  `grok-companion.mjs` exactly like the other commands (single stdout envelope; relay on
  stderr).

### Component B - Live-knowledge grounding + citations

- **Defaults:** live search ON by default for `adversarial-review` and `reason`; OPT-IN
  via `--web` for `review` and `code`; `--no-web` forces it off everywhere. These are
  wrapper-level defaults so every entrypoint (skill, plugin, rescue subagent) inherits
  them identically (DRY - one default table, not per-command copies).
- **Citations capture (the one real unknown):** how Grok 4.5 surfaces its search sources
  inside `streaming-json` is NOT assumed. The plan's Task 0 is a live grounding probe
  against real Grok that captures the actual event shape for a web-grounded run. Two
  outcomes are designed for:
  1. **Structured sources in the stream** (preferred): the relay extracts them and the
     mode assembles a `citations` array directly.
  2. **Fallback - instructed Sources block:** if the stream does not carry structured
     sources, the prompt instructs Grok to end its answer with a machine-parseable
     `Sources:` block (one `- <url> | <title> | <what-it-grounded>` per line) that the
     mode parses. The fallback is deterministic and testable without live network.

  The plan MUST implement whichever the probe proves, and keep the parser isolated in one
  module (`groklib/citations.py`) with unit tests over captured fixtures - no live
  network in the test suite.
- **Envelope field:** add `response.citations: Citation[]` where
  `Citation = { url: string, title: string, grounded: string }` (`grounded` = one line on
  what this source was used to claim). The field is optional and absent when a run was not
  grounded. This is a strictly additive C4 extension; Phase 2 consumers ignore unknown
  fields.
- **Fail-closed:** if grounding was requested (default-on mode, or explicit `--web`) but
  the run produced zero citations, the envelope records a warning
  (`grounding-requested-no-sources`) and the command surfaces it - we never render a
  "grounded" result that silently had no sources.
- **Secret hygiene:** citations pass through the same `assert_no_secret_material` /
  `redact_secret_material` path as the rest of the envelope. A URL carrying a token-shaped
  query string is redacted, not emitted raw.
- **Rendering:** the command output ends with a `Sources` section listing the citations;
  where a finding references a source, an inline marker points at it.

### Component C - Zero-config UX

- **Auto-preflight:** every command runs a fast readiness check (accepted version, auth
  reachable, sandbox enforceable) before doing work. A positive result is cached briefly
  (a small `preflight-cache.json` under the state root with a short TTL and the resolved
  Grok version as the cache key) so repeated commands do not re-probe. On failure the
  command fails fast with the exact remediation (run `/grok:setup`), never a raw stack.
  The cache is fail-closed: a missing, stale, malformed, or version-mismatched cache means
  re-run preflight, never assume-ready.
- **One-command setup:** `/grok:setup` performs the whole path - detect the installed Grok
  version, accept/pin it (`accepted-version.json`), verify auth reachability, verify the
  sandbox is enforceable on this platform, optionally toggle the Stop-review gate - and
  prints a single green readiness summary (or the first blocking problem with its fix).
- **Cleaner streaming + report:** the relay tags each surfaced line by kind
  (thought / tool / status) so the live view reads cleanly, and grounded review /
  adversarial-review output renders as a report grouped by severity with the Sources
  section appended. This is presentation only - the envelope contract is unchanged beyond
  the additive `citations` field.

---

## 3. Contracts and interfaces (locked before phased steps)

- **C-A1 (envelope citations):** `response.citations?: { url, title, grounded }[]`.
  Optional; absent when ungrounded. Additive to C4; validated by an extended envelope
  validator; covered by envelope-equivalence tests so Phase 2's guarantees hold.
- **C-A2 (grounding warning):** `warnings` MAY include `grounding-requested-no-sources`.
  One source of truth for the warning string.
- **C-A3 (wrapper flags):** `--web` / `--no-web` resolve against a single default table
  keyed by mode. `adversarial-review` and `reason` default true; `review` and `code`
  default false. The table is the sole authority; commands never re-encode defaults.
- **C-A4 (adversarial mode):** shares `review`'s target-resolution, worktree handling, and
  envelope assembly; differs only in the prompt stance and the grounding default. No
  duplicated review logic.
- **C-A5 (citations parser):** `groklib/citations.py` exposes one parse entrypoint used by
  both the streaming path and the Sources-block fallback, returning `Citation[]`. Pure,
  unit-tested over fixtures, no network.
- **C-A6 (preflight cache):** `preflight-cache.json` = `{ version, checkedAtMs, ok }`.
  Consumed only through one loader that re-validates version + TTL and fails closed.
- **C-A7 (command contract):** every new/changed command still yields exactly one stdout
  JSON envelope from the wrapper; the relay and hooks write only stderr.

---

## 4. Decisions

- **D-W1-GROUND (grounding default):** grounded-where-it-matters. `adversarial-review` and
  `reason` default-on; `review` and `code` opt-in; `--no-web` overrides. Chosen for the
  best mix of "knows today's internet" and predictability/cost/reproducibility.
- **D-W1-ADV-OUTPUT:** adversarial-review returns severity-ranked attacks with concrete
  reproductions + citations + a brief one-line fix hint; NOT full fixes (keeps it an
  adversary, distinct from `review`).
- **D-W1-CITE-CAPTURE:** citation capture mechanism is decided by a live probe (plan Task
  0), with a deterministic instructed-`Sources:`-block fallback. Parser isolated + fixture
  -tested; no live network in CI.
- **D-W1-DOCS-LOCATION:** Wave 1 spec/plan and the Wave 2/3 roadmap live under
  `docs/` alongside the plugin and wrapper in this repository.
- **D-W1-STACK:** Wave 1 lands on a dedicated feature branch; Phase 1+2 contracts stay
  frozen; all Wave 1 changes are additive.
- **D-W1-FAILCLOSED-GROUND:** a grounded run with zero sources is surfaced as a warning,
  never rendered as silently-grounded.

---

## 5. Risks and verification

- **R1 - citation surfacing (primary):** mitigated by the Task 0 live probe + the
  fallback. The design does not depend on either outcome; the plan implements whichever
  the probe proves.
- **R2 - determinism of grounded runs:** grounded modes are less reproducible by nature;
  we accept this only for the two default-on modes and keep `review`/`code` deterministic
  by default. Tests never hit the network (fixtures only).
- **R3 - secret leakage via URLs/snippets:** mitigated by routing citations through the
  existing secret-scan/redaction before stdout.
- **R4 - preflight cache staleness after a Grok daily release:** mitigated by keying the
  cache on the resolved version and failing closed on mismatch.

Verification: the plan is TDD throughout; a live end-to-end validation (real Grok) mirrors
Phase 2's V1-V4 - grounded `adversarial-review` on a disposable repo producing real
citations, `reason` grounded, `review`/`code` ungrounded-by-default, auto-preflight cache
hit/miss, and the fail-closed no-sources path.

---

## 6. Testing approach

- Python: unit tests for the citations parser (both stream + Sources-block fixtures), the
  grounding default table, the preflight cache loader (missing/stale/malformed/version-
  mismatch all re-probe), the adversarial mode's prompt/grounding wiring, and envelope
  equivalence (Phase 2 guarantees preserved with the additive field).
- Node: the command wiring for `adversarial-review`, the relay's kind-tagging, and the
  report/Sources rendering.
- Live: one gated end-to-end pass against real Grok (not in CI).
- Full Phase 2 suite (291 py + 46 node) stays green; no Phase 2 contract regressions.

---

## 7. Global constraints (apply to every task)

- Python stdlib only (3.9 floor); Node stdlib only. No pip/npm deps.
- Every file starts with its repo-relative path-header comment.
- No `as`/`as const`/`!` casts (TS side); type properly. No `as any`.
- No empty catch, no swallowed errors: log with context + explicit action.
- Fail closed everywhere (grounding, preflight cache, citation parse, sandbox).
- DRY: one source of truth for the grounding default table, the citations parser, the
  warning strings, and the envelope citations shape.
- No em-dashes or en-dashes anywhere (prose, comments, commits). ASCII hyphens only.
- No new code file over 900 lines; split by responsibility.
- Additive-only to Phase 2 contracts; no compatibility shims (none needed - additive).
- Every prompt written as positive MUST/DO instructions.

---

## 8. Out of scope for Wave 1 (see roadmap)

Multi-run orchestration (debate / red-team) is Wave 2. Long-running autonomous background
workers are Wave 3. Both are captured in `docs/roadmap.md` and are NOT built
here.
