# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for marketplace / package tags.

## [1.3.0] - 2026-07-16

### Added

- **Durable run lifecycle:** seed `run.json` (lifecycle `created`, status
  `running`, `recordRevision` 0) before run-id publication; exclusive
  `run.lock` + compare-and-swap record updates; envelope-first
  `persist_terminal_envelope` (idempotent lifecycle finish; never replace a
  valid terminal envelope).
- **Spawn finalization worker** with parent recovery only when the worker is
  confirmed not alive (`finalization-timeout` /
  `finalization-worker-missing-result` / ephemeral `finalization-worker-unkillable`).
- Progress events carry process-local monotonic `elapsedMs` and UTC `ts`.

### Changed

- **`/grok:status` projection:** strictly read-only; effective lifecycle from
  record → valid envelope → derived `interrupted` (dead owner, no envelope).
  Failed/canceled/interrupted targets return top-level `failure` and exit 1
  while still emitting a well-formed status envelope. `response.target`
  includes `lifecycle`, `lifecycleSource`, and `elapsedMs`.

## [1.2.10] - 2026-07-15

### Fixed

- **`/grok:status` in-progress UX:** when a run is still going (no
  `envelope.json` yet, owner process alive), status returns top-level
  `"status": "running"` with `response.target` (elapsedSeconds, process,
  eventCount, lastEvent, recentEvents) instead of pretending success and
  warning about a missing stored envelope. Exit 0 for both `success` and
  `running`. Companion no longer re-dumps progress to stderr after the JSON
  (hosts that merge streams were gluing `[grok] …` onto the envelope).

## [1.2.9] - 2026-07-15

### Fixed

- **Docs matched code:** README, PROVENANCE, authority policies, wrapper SKILL,
  checklists, roadmap, live probe docs, and setup hints now match behavior —
  no hard CLI pin, repo-agnostic targets, review drift as warnings, reason web
  default off, Codex `agents/run.mjs` / `GROK_AGENT_RUN`, dual-host surface.

## [1.2.8] - 2026-07-15

### Fixed

- **No hard Grok CLI version lock:** runtime accepts any working
  `grok --version`. `accepted-version.json` is last-validated maintainer
  evidence only (`enforcement: none`), not a user allowlist. Exact build
  mismatch no longer fails closed as `version-mismatch`.

## [1.2.7] - 2026-07-15

### Fixed

- **Entry-derived plugin root wins over stale env:** `skills/*/run.mjs`,
  `agents/run.mjs`, `grok-companion.mjs`, and SessionStart always bind
  `CLAUDE_PLUGIN_ROOT` / `PLUGIN_ROOT` to the install tree they live in so a
  leftover env after marketplace upgrade cannot mix old/new wrappers.
- **Wrapper override is opt-in:** `GROK_AGENT_WRAPPER` is ignored unless
  `GROK_ALLOW_WRAPPER_OVERRIDE=1` (tests/advanced only).
- **Codex agent writes are atomic** (`*.tmp` + rename); SessionStart timeout
  raised to 30s so agent materialize is less likely to be cut off.
- **Incomplete-stop salvage hardened:** empty shells (`findings: []` /
  `findings: null` / placeholder-only) are not salvaged; turn-exhaustion only
  when the operator set `--max-turns`; schema-invalid structured on incomplete
  runs is cleared and warned on the envelope (`incomplete_warnings` →
  `warnings`); failure envelopes keep incomplete notes when `response` is kept.
- Docs scrub: SessionStart zero post-install, `agents/run.mjs` /
  `GROK_AGENT_RUN`, model-invocation note, unlimited max-turns defaults.

## [1.2.6] - 2026-07-15

### Fixed

- **No default max-turns:** review/reason/code/verify omit `--max-turns` unless
  the operator sets it. Unlimited by default (subscription Grok CLI).
- **Cancelled with findings is not a wipe:** if Grok stops with `Cancelled` (or
  hits an explicit turn budget) but produced text/structured output, the wrapper
  returns **success** with a warning and keeps findings (`response` populated).
- **Turn-cap as Cancelled:** when `--max-turns` is set, Grok's observed
  `stopReason: Cancelled` at the budget classifies as turn-exhaustion (or
  salvage if content exists), not a silent user-cancel.
- Capture `num_turns` from more end-event field shapes into classification.

## [1.2.5] - 2026-07-15

### Fixed

- **Agents aligned with skills:** shared self-locating `agents/run.mjs` (same
  `skill-run` family as `skills/*/run.mjs`). Claude agents use
  `$PLUGIN_INSTALL/agents/run.mjs`; Codex managed TOML injects absolute
  `GROK_AGENT_RUN` to that runner (not bare companion-only env).

## [1.2.4] - 2026-07-15

### Fixed

- **Transparent Skill-tool entry:** every skill ships `skills/<name>/run.mjs` that
  self-locates the plugin root from its own path and spawns the companion.
  Model contract is only `node "$SKILL_BASE/run.mjs" <mode> …` where `SKILL_BASE`
  is the Skill tool base directory (no env, no invented cache versions).
  Shared: `scripts/lib/skill-run.mjs`. Docs: `plugin/references/plugin-root.md`.
  Also: `resolve-plugin-root` helpers/CLI for tests and advanced use.

## [1.2.3] - 2026-07-15

### Added

- Release process checklist: [docs/RELEASE.md](docs/RELEASE.md) (linked from README, AGENTS.md, CONTRIBUTING.md)
- [plugin/references/plugin-root.md](plugin/references/plugin-root.md): never invent cache paths; Codex agent uninstall
- Setup `--remove-codex-agents` (managed agents only, with `*.bak` backups)

### Fixed / improved

- Claude agents: `tools: Bash(node:*)` only; clearer rescue vs engineer-coder routing
- Codex agent TOML: `sandbox_mode = "read-only"`, never-invent-paths, absolute companion
- Managed agent updates create backups before overwrite
- Injection tests cover all Claude agents + Codex TOML templates
- **Skills allow model invocation by default:** removed `disable-model-invocation`
  from all `/grok:*` skills so Codex (and Claude Skill tool) can invoke them.
  Users still control when Grok runs; slash commands keep working.

## [1.2.2] - 2026-07-15

### Fixed

- **Review UX: stop discarding finished reviews for purity checks.**
  - Tree drift during the run → informational warning only
  - Grok listing change-shaped JSON keys → informational warning only
  - Pre-run FS baseline capture failure → soft-skip; review still runs
  - Findings always kept when Grok completed successfully
  - `unexpected-edits` remains for `code`/`verify` worktree escapes only
  - Audit: `docs/reviews/2026-07-15-over-conservatism-audit.md`

## [1.2.1] - 2026-07-15

### Fixed

- **Zero post-install for Codex agents:** `SessionStart` auto-installs managed
  agents into `~/.codex/agents/` with an **absolute** path to `grok-companion.mjs`
  (no `PLUGIN_ROOT` required at spawn). *(Agent entry path superseded by 1.2.5:
  absolute `agents/run.mjs` / `GROK_AGENT_RUN`.)* Manual `/grok:setup` is optional
  (readiness / gate / mode only).
- Managed agents refresh when the plugin cache path or templates change; user-owned
  TOML (no `managed-by: grok-skills` header) is left alone unless `--force-codex-agents`.
- Setup exit code fails when agent ensure fails (unless `--skip-codex-agents`).

## [1.2.0] - 2026-07-15

First public release of **grok-skills**: dual-host Grok companion for Claude Code and Codex.

### Added

- Hardened Python wrapper (7 modes, envelope, progress stream, worktree isolation)
- Dual packaging: Claude marketplace + Codex `.agents` marketplace; wrapper under `plugin/wrapper/`
- Skills: preflight, setup, review, adversarial-review, reason, code, verify, debate, dual-lens, jobs, result, cancel, transfer, status, cleanup
- **Agents:** `grok-engineer-coder` (implementer; host orchestrates) and `grok-rescue` (diagnosis)
- Setup installs Codex agents into `~/.codex/agents/`; Claude loads `plugin/agents/` automatically
- Job registry, dual run modes (hardened / direct), optional fail-closed stop-review gate
- Preflight cache, citations, web defaults, transfer allowlist, workspace session stamps
- SECURITY.md, AGENTS.md, CONTRIBUTING.md, CI (Python 3.11/3.12, Node 20/22, packaging checks)

### Security

- Fail-closed stop gate (structured findings / verify pass; forces hardened)
- Progress redact-on-write; secret patterns + injected-auth denylist
- Gate-scripts-modified hard fail; git hooks disabled on worktree ops
- Honest trusted-input model and residual limits (D-SECRETREAD, D-NET, D3)
