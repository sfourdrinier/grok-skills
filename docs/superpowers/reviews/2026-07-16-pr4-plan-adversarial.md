# PR4 plan adversarial review (pre-execution)

**Date:** 2026-07-16  
**Branch:** `feat/pr4-implementation-handoff-1.6.0`  
**Authority:** design §14 + plan PR4 tasks 4.0–4.11 (pre-update)  
**Grounding:** live modules on main/1.5.0 tip; multi-agent worktree practice (2026)

## Product north star

Grok is a **peer implementer** for Claude Code and Codex multi-agent loops: isolated work, verified artifacts, parent integrates. Handoff is the integration API; notify is only a completion signal.

## What already matches 2026 multi-agent practice

| Practice | Design/plan coverage |
|----------|----------------------|
| Git worktree isolation per agent | Existing `code` external worktree + PR4 retains it |
| Spec/contract-scoped write bounds | `--contract-file` writeScopes |
| Automated gates before “merge/integrate” | requiredValidation + build gate + shared safety |
| Immutable artifact trail | Phase-1 binary full-index patch + SHA-256 + phase-2 manifest |
| Parent re-validates after integrate | Explicit in §14.10 / parent protocol |
| No auto-apply / auto-commit | Locked non-goal for PR4 |
| Parallel peers need disjoint scopes | §14.16 |
| Binary patches need full index | §14.7 `git-binary-full-index-v1` (matches git-apply requirements) |

Industry pattern (worktrees + gates + review before integrate) is aligned; PR4 adds what most tools lack: a **machine-checkable ready handoff** keyed by `runId`.

## Grounding facts (code today)

| Fact | Evidence |
|------|----------|
| No `handoff` mode yet | `envelope.MODES` ends at cleanup; companion WRAPPER_MODES has no handoff |
| No PR4 ERROR_CLASSES yet | Missing `implementation-contract-invalid`, `write-scope-violation`, `unexpected-commit`, `artifact-*`, `handoff-unavailable`, `terminal-envelope-incomplete` |
| `secret-material` not an ERROR_CLASS | Exists as design **blocker**; envelope uses `SecretMaterialError` / validation-failure paths - map carefully |
| Code finalization is hook-based | `_worktree.run_worktree_mode` + `FinalizeStage` + code finalize - **must extend this order**, not invent a second finalize pipeline |
| Sentinel / build gate / escape already exist | `code.py` `_assert_cwd_sentinel`, `_execute_build_gate`, gate-scripts-modified |
| Lifecycle primitives ready | `create_run`, `persist_terminal_envelope`, CAS |
| Companion under 900 lines after PR3 | ~861 lines; handoff must stay thin passthrough like status |
| Dual-host skills | `run.mjs` self-locating; agents need handoff pointer after code |

## Adversarial findings (plan gaps)

### F1 - Dual-host parent playbook under-specified for “peer” claim (P1)

Design §14.15 is “document only.” For peer loops, **Claude + Codex** skills/agents must know: after code success → poll status → `/grok:handoff --run-id` → only if dual-condition ready → parent apply protocol.  
**Plan fix:** Task 4.8/4.11 require parent protocol in `handoff/SKILL.md`, `code/SKILL.md`, `agents/grok-engineer-coder.md`, codex-agents TOML, references README; dual-host smoke is mandatory not optional prose.

### F2 - Direct mode interaction undefined (P1)

Direct mode skips durable wrapper runs for notify. Code may still run; handoff needs worktree + artifacts under state root.  
**Plan fix:** Handoff artifacts **require hardened code path** (external worktree). Direct + contract/handoff: fail closed with clear usage error, or document “no handoff in direct” and keep confinement-only. Prefer **fail closed** when `--contract-file` or handoff expectation is present.

### F3 - Second finalization pipeline risk (P1)

Code already has prepare → execute → finalize via `FinalizeStage`. Duplicating order in a new parallel function will drift.  
**Plan fix:** Task 4.3 must **extend** code finalize / one ordered function called from existing finalize hook; test spies order on that path only.

### F4 - Companion size / entrypoint discipline (P1, PR3 lesson)

Adding handoff logic into `grok-companion.mjs` will re-break the 900-line cap.  
**Plan fix:** `runHandoff()` thin passthrough only (mirror `runStatus`); no job/notify/relay; test enforces companion ≤900 lines and no Grok spawn.

### F5 - ERROR_CLASSES vs blocker strings (P1)

Seven ERROR_CLASSES listed; `secret-material` and `temp-index-retained` and `no-changes` are **blockers** in handoff JSON, not necessarily top-level envelope classes. Primary envelope class mapping is underspecified.  
**Plan fix:** Explicit mapping table in plan Task 4.0 / design cross-link: which blocker becomes envelope `error.class` when primary.

### F6 - Contract file read trust (P2)

Contract is operator-trusted content, but path comes from CLI. Symlink / path escape when **reading** the contract file can pull unexpected host content into trust boundary.  
**Plan fix:** Resolve contract path, reject if not a regular file, reject symlink-to-escape if outside allowed roots (operator cwd/repo), document parent supplies path under repo or explicit absolute trusted path.

### F7 - Notify vs handoff confusion (P2)

With 1.5.0 notify, parents may treat toast as “ready to integrate.” Notify only means terminal attempt; **ready** is handoff dual-condition only.  
**Plan fix:** code/handoff skills + COMPATIBILITY: notify is optional signal; always call `/grok:handoff` before integrate; envelope `runId` is the key.

### F8 - Schema discoverability without dual JSON Schema (P2)

Design forbids a second public JSON Schema file (DRY). Parents still need a stable shape.  
**Plan fix:** Single markdown schema section in `plugin/references/implementation-handoff.md` generated from / mirroring `validate_implementation_handoff` fields; round-trip test remains source of truth.

### F9 - Parent apply protocol operational detail (P2)

2026 practice still uses worktrees + review before integrate. Parent protocol needs: base still present, dirty overlap check, `git apply --check --binary`, explicit apply, revalidate.  
**Plan fix:** Keep document-only apply in PR4 but expand parent checklist with exact git argv and failure handling; dual-host smoke exercises check (not necessarily live apply on CI).

### F10 - Parallel peers / monorepo reality (P2)

One-target PR4 is correct for v1, but peer loops often span packages.  
**Plan fix:** Document sequential runs or single umbrella target; no multi-root gates in PR4; skill warns when writeScopes span multiple package managers without one gate root.

### F11 - Patch size and monorepos (P3)

25 MiB default may be tight for binary-heavy trees.  
**Plan fix:** Keep env clamp; document; test oversized → fail closed `artifact-generation-failure` (or blocker), never truncate.

### F12 - Windows path / apply (P3)

Parent may apply on Windows; wrapper often macOS. Patch format is git binary portable if full-index.  
**Plan fix:** Tests with odd paths (`-z`); document parent git version requirements for apply --binary.

### F13 - Quality bar from PR3 (P1 process)

Matrix → DRY → suites → internal review → package. Missing-flag, path-secret display, companion split lessons apply.  
**Plan fix:** Gate A includes companion thinness, handoff flag missing-value, no secret paths in handoff envelope stdout; Gate D required before 1.6.0 tag.

## Explicit non-goals (reaffirmed)

- Auto-apply / auto-commit / merge / push  
- OS-sandbox of `requiredValidation`  
- Exactly-once notify or handoff delivery  
- Multi-root build gates in one run  
- Windows native toast  
- Replacing parent VCS policy  
- **ACP (Agent Client Protocol)** client/server in this plugin (Grok CLI may speak ACP for IDE hosts; PR4 handoff is orchestrator peer artifacts, not ACP transport)  

### F14 - Grok CLI ACP (informational; no plan change)

Grok Build/CLI advertises full **ACP** support (IDE↔agent JSON-RPC; registry entries
such as Zed “Grok Build”). That is complementary to grok-skills:

- **ACP path:** editor drives Grok agent session live.  
- **Plugin path:** Claude/Codex orchestrate a **sandboxed** headless Grok run and
  (PR4) pull a **verified handoff** by `runId`.

Do not fold ACP into PR4 scope or delay handoff for ACP parity.

## Verdict

**Plan is strong and still the right shape for peer handoff.** Do **not** execute Tasks 4.1+ until plan is updated for F1–F13 (especially F1–F5, F13). Design §14 remains authority; plan gains dual-host parent wiring, finalize-hook integration, ERROR_CLASS mapping, contract-path hygiene, notify/handoff separation, companion thinness.

## Recommended execution order after plan update

1. Task 4.0 matrix + DRY names (expanded)  
2. Contract module → unexpected-commit → finalize order (into existing hook)  
3. Phase-1 patch → validation → evidence → phase-2  
4. handoff mode + skills/agents dual-host  
5. cleanup warning → Gate D → docs/package 1.6.0  
