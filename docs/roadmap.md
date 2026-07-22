<!-- docs/roadmap.md -->

# Grok Skills roadmap

Goal: match (and beat) the UX of OpenAI's official Codex-for-Claude plugin
(`openai/codex-plugin-cc`), while shipping a dual-host Grok companion for
**Claude Code** and **Codex / ChatGPT desktop**, with an optional security
posture.

Unifying idea: Grok as a live second mind you can pull into either harness.

---

## Security postures (both supported)

| Mode | Env / flag | Behavior |
|------|------------|----------|
| **hardened** (default) | `GROK_SKILLS_MODE=hardened` or omit | Private auth home, OS sandbox verify, mode-aware isolation (worktree for code auto/review and all ACP peer; live tree for code direct), secret redaction, gate-script integrity. |
| **direct** | `GROK_SKILLS_MODE=direct` or companion `--run-mode direct` | Use the **installed Grok CLI** with its normal home/auth (parity with "use your installed Codex"). Faster setup; you accept Grok's own isolation model. |

Setup and skills document both. Hardened stays the default for open-source
trust marketing; direct is one flag away for people who already live in Grok.

---

## Shipped foundation

- Hardened Python wrapper (7 modes + envelope + progress stream)
- Claude + Codex dual packaging (wrapper bundled inside plugin install tree)
- Skills surface, rescue agent, opt-in stop gate, dual-env hooks
- Tests: wrapper unit suite + plugin unit suite

---

## Product surface (parity with codex-plugin-cc + beyond)

### A. Job lifecycle (parity)

| Item | Command / skill | Status |
|------|-----------------|--------|
| Track runs as jobs | automatic on live modes | **shipped** |
| List jobs (table) | `/grok:status` without run-id, `/grok:jobs` | **shipped** |
| Durable lifecycle + CAS + finalize worker | wrapper `run.json` / spawn finalize / status projection | **shipped (1.3.0), hardened (1.3.1)** |
| Completion notifications | companion push on terminal live runs (hardened durable runs) | **shipped (1.5.0)** |
| Verified implementation handoff | contract + patch + `/grok:handoff` dual-condition ready | **shipped (1.6.0)** |
| Notify dogfood follow-ups | operator re-attempt; direct-mode signal; headless honesty | **PR5 → 1.7.0** |
| Opt-in isolated review | `review --isolated` owned worktree + tracked dirty; `--base` stays live | **shipped (1.4.0)** |
| Fetch finished output | `/grok:result [job-id]` | **shipped** |
| Cancel running job | `/grok:cancel [job-id]` | **shipped** |
| Human render | `/grok:result --pretty` / companion render | **shipped** |

### B. Review UX (parity + better)

| Item | Notes | Status |
|------|-------|--------|
| Default target | working tree / `.` when `--target` omitted | **shipped** |
| `--base <ref>` | branch-oriented review framing (does not force isolation) | **shipped** |
| `--isolated` | opt-in owned worktree review (HEAD + tracked dirty) | **shipped (1.4.0)** |
| `/grok:adversarial-review` | steerable challenge review + focus text | **shipped** |
| Structured schema | `schemas/review-output.schema.json` optional | **shipped** |
| Web-grounded adversary | `--web` default-on for adversarial (Wave 1) | **shipped** (live polish open) |

### C. Delegation / continuity (parity)

| Item | Notes | Status |
|------|-------|--------|
| Rescue resume | `--resume` / `--fresh` last rescue thread | **shipped** (agent template) |
| Transfer | Claude transcript → Grok task pack | **shipped** |
| SessionStart hook | stash transcript path for transfer + auto-ensure Codex agents | **shipped** (1.2.1) |

### D. Setup (parity)

| Item | Notes | Status |
|------|-------|--------|
| Rich setup report | CLI present (any working build), auth, mode | **shipped** |
| Install guidance | Grok CLI + **git marketplace install** (no silent binary install) | **shipped** |
| Zero post-install Codex agents | SessionStart materializes `~/.codex/agents` with absolute `agents/run.mjs` (`GROK_AGENT_RUN`) | **shipped** (1.2.1; runner path 1.2.5+) |
| Toggle run mode | hardened vs direct persisted per workspace | **shipped** |
| Preflight cache | installed-version-keyed short-circuit (`preflight-cache.json`; not a hard pin) | **shipped** |
| No hard CLI version lock | any working `grok --version`; stamp advisory only | **shipped** (1.2.8) |

### E. Beyond OpenAI's plugin

| Item | Notes | Status |
|------|-------|--------|
| Dual host (Claude + Codex plugin) | foundation | **shipped** |
| Worktree-first `code` | keep isolation; optional apply later | **shipped** |
| `/grok:debate` | multi-round bounded | **shipped** (v1) |
| Dual-lens harden recipe | `docs/dual-lens-harden.md` + `/grok:dual-lens` | **shipped** |
| Citations in envelope | Sources-block + stream harvest; warning if empty | **shipped** |

---

## 2.0.0 peer-agent integration (in progress on `feat/2.0-peer-agent`)

Plan: `docs/superpowers/plans/2026-07-16-peer-agent-integration.md`. Live
evidence: `docs/checklists/2.0-live-smoke-ledger.md`. One release (2.0.0),
phases as sequential PRs; most implementation delegated to Grok through this
plugin's own contract -> code -> handoff pipeline.

| Phase | Content | Status |
|-------|---------|--------|
| 0 (PR6) | Hygiene: verify/checks gate + CI, task-file DRY, argv-safety reference, divergence warning, test splits, review fixes | **done 2026-07-17** |
| 1 (PR7) | Acceptance criteria wired, `implement` combo, unified IDs, direct-mode parity | **done 2026-07-17** |
| - | Phase 1 adversarial review: 6 findings fixed (exit contract, id collision, summary caps, fencing) | done |
| 2 (PR8) | Iteration loop: session archive + `code --continue-run` | **done 2026-07-17** |
| 3 (PR9) | Claude Code native surface (bin/, plugin data dir, SubagentStop, userConfig, agent frontmatter) | **done 2026-07-17** |
| 4 (PR10) | Codex parity polish | **done 2026-07-17** |
| 5 (PR11) | ACP probe + spec + peer channel (default; opt out with `GROK_DISABLE_ACP=1`; `GROK_EXPERIMENTAL_ACP` no longer a hard gate) | **done 2026-07-17** |
| 6 (PR11) | Manifest polish + drift guard + version bump; tag pending maintainer go | **done 2026-07-17** (tag still maintainer-gated) |
| 7 | Peer-native re-architecture: integration=direct default (consent removed in 2.0.1), auto/review opt-in worktrees, ACP default peer channel, mode-aware integrate, honest docs | **shipped** on 2.0.x; dual-host installed-host smoke + tag still maintainer-gated. Plan: `docs/superpowers/plans/2026-07-17-phase7-peer-native.md`; design: `docs/specs/2026-07-17-peer-native-integration-design.md`; live evidence: `docs/checklists/2.0-live-smoke-ledger.md` |

Phase 7 locks product defaults (not a silent flip):

- **integration=direct** (product default name) = for one-shot **code** and **peer-stop** landing, live-tree / stop-time apply (**no consent gate** as of 2.0.1); **implement always forces worktree + verify-only** and never live lands
- **auto / review** = for one-shot code, opt-in isolated worktrees (apply-on-ready vs parent apply); for ACP peer, same stop-time land/retain choice after always-external isolation
- **ACP** = default multi-turn peer channel for `grok-engineer-coder` (opt out with `GROK_DISABLE_ACP=1`); one-shot `code` is fallback; peer is never live-edit of the operator tree during prompts
- **runMode direct** remains a **separate** installed-home security posture (orthogonal to integration; peer is hardened-only)
- **Shared auto/peer apply spine** (exclusive apply lock + durable marker + header-union dirty set + fail-closed no automatic reclaim) + peer-stop final-envelope / durable-terminal / single-flight lifecycle honesty; peer-stop **not** completion-notification eligible (see CHANGELOG Phase 7 final-review Fixed)

Canonical mode matrix (do not restate here): [plugin/references/integration-modes.md](../plugin/references/integration-modes.md).

## Recommended next order (after Wave 1 polish)

1. ~~**Live validation**~~ done 2026-07-15 (`docs/checklists/wave1-live-results-2026-07-15.md`)
2. **Phase 7 release gate** (dual-host installed-host smoke + tag readiness) on `feat/2.0-peer-agent` after final-review docs-follow-code
3. **DRY consolidation (post-2.0)** - [issue #6](https://github.com/sfourdrinier/grok-skills/issues/6): eliminate dual sources (peer lease constant, workspace state segment, flag-presence SSOT, runner lifecycle helpers, ACP C6 pins, dual-host agent content parity). **TDD required; no regressions; one PR.** Do not grow known dual sources while this is open.
4. ~~**Linux sandbox profile** when a probe report exists~~ **done 2.0.1** (`probe-report-linux.md`, `PROBED_PLATFORMS` includes `linux`, `linux/landlock`, bwrap prereq)
5. Optional apply-worktree UX; official directory listings

---

## Out of scope for first open-source tag (explicit)

- Auto-installing the Grok binary without user consent  
- Claiming network/read sandbox beyond what the current Grok CLI + platform probe evidence enforce  
- Full multi-agent supervisor (Wave 3 autonomy)  

Those remain backlog, not silent promises.
