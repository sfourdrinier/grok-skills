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
| **hardened** (default) | `GROK_SKILLS_MODE=hardened` or omit | Private auth home, OS sandbox verify, worktree isolation, secret redaction, gate-script integrity. |
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
| Fetch finished output | `/grok:result [job-id]` | **shipped** |
| Cancel running job | `/grok:cancel [job-id]` | **shipped** |
| Human render | `/grok:result --pretty` / companion render | **shipped** |

### B. Review UX (parity + better)

| Item | Notes | Status |
|------|-------|--------|
| Default target | working tree / `.` when `--target` omitted | **shipped** |
| `--base <ref>` | branch-oriented review framing | **shipped** |
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

## Recommended next order (after Wave 1 polish)

1. ~~**Live validation**~~ done 2026-07-15 (`docs/checklists/wave1-live-results-2026-07-15.md`)
2. **Linux sandbox profile** when a probe report exists  
3. Optional apply-worktree UX; Wave 3 multi-agent; official directory listings

---

## Out of scope for first open-source tag (explicit)

- Auto-installing the Grok binary without user consent  
- Claiming network/read sandbox beyond what the current Grok CLI + platform probe evidence enforce  
- Full multi-agent supervisor (Wave 3 autonomy)  

Those remain backlog, not silent promises.
