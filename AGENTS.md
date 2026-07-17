# AGENTS.md - grok-skills

Dual-host **plugin** (Claude Code + Codex) that runs **Grok** as a second coding mind via a **hardened stdlib Python wrapper**. Not affiliated with xAI. Install unit is `plugin/`; marketplace roots are repo-level (`.claude-plugin/`, `.agents/plugins/`).

## Non-negotiables

1. **Docs follow code.** Every behavior, install, flag, skill, envelope, or security change updates **all** of: `README.md`, `CHANGELOG.md`, relevant `docs/**`, skill `SKILL.md`s, `plugin/references/**`, and roadmap status when applicable. Stale docs = incomplete change.
2. **Everything is DRY.** Never copy-paste logic, tables, patterns, prompts, or contracts. One source of truth; extract shared helpers; if it exists twice, delete one and call the other. Known single sources include: web defaults, citations parse, preflight cache, envelope field specs, secret patterns, run lifecycle, tool allowlists.
3. **Proper tests, always.** Every behavior change has real unit coverage (not smoke-only). Prefer TDD for contracts. Wrapper: `python3 -m unittest discover -s tests -q` in `plugin/wrapper/scripts`. Plugin: `node --test tests/*.test.mjs` in `plugin/scripts`. Tests fail closed on the same invariants as production.
4. **Wrapper owns safety.** All sandbox, auth-home, secret redaction, worktree isolation live in `plugin/wrapper/`. Plugin/companion is thin: resolve root, pass argv, relay **one** JSON envelope on stdout **verbatim**.
5. **Fail closed.** Unverified platform, sandbox, stream, or cache → classified failure, never silent success. Do **not** fail closed on Grok CLI build string mismatch.
6. **Stdlib only.** Python 3 stdlib + Node stdlib. No new runtime deps without explicit maintainer OK.
7. **Self-contained install.** Wrapper stays under `plugin/wrapper/` so marketplace cache installs work. No `../shared` that dies on install.
8. **No secrets in source.** Never commit real credentials. Test fixtures must not hold contiguous secret-shaped literals (split strings). Scrub history if anything leaks.
9. **No personal / monorepo paths** in committed code or docs. Use placeholders.
10. **One stdout envelope** per run. Progress/relay/hooks → stderr only.
11. **900-line file cap.** Split by responsibility; keep path-header comments on code files.
12. **ASCII hyphens only** in prose/comments/commits (no em/en dashes).
13. **Dual-host parity.** Claude + Codex manifests/skills stay aligned; document both install paths (`sfourdrinier/grok-skills` preferred; local path = dev only).
14. **No hard CLI version lock for users.** Runtime accepts any working `grok --version`. `accepted-version.json` is last-validated maintainer evidence only (`enforcement: none`). Update the stamp after a full probe suite if you want docs accuracy - never as a user-facing allowlist.
15. **Trusted-input model.** Document limits honestly; do not claim read/network sandbox beyond what the current Grok CLI + platform actually enforce.
16. **Releases follow the checklist.** Tag/publish only via [docs/RELEASE.md](docs/RELEASE.md): bump all packaging versions, CHANGELOG, tests, annotated `vX.Y.Z` tag, GitHub Release, dual-host post-smoke.

## Layout (cheat sheet)

| Path | Role |
|------|------|
| `plugin/wrapper/` | Hardened engine (`grok_agent.py`, `groklib/`) |
| `plugin/scripts/` | Companion, relay, hooks (Node) |
| `plugin/skills/` | `/grok:*` skill docs |
| `docs/` | Specs, roadmap, checklists, security decisions |
| `.claude-plugin/`, `.agents/plugins/` | Marketplace manifests |

## Before you finish

- [ ] Behavior matches docs listed above  
- [ ] No duplicated logic (DRY); single sources still single  
- [ ] Proper unit tests for the change; suites green  
- [ ] No secret-shaped contiguous literals; no private paths  
- [ ] `claude plugin validate ./plugin --strict` if packaging changed  
- [ ] If shipping a public version: follow [docs/RELEASE.md](docs/RELEASE.md)  

## Releases

Maintainer publish path (version files, tag, `gh release`, Codex/Claude smoke):
**[docs/RELEASE.md](docs/RELEASE.md)**.
