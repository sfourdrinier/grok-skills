# Release process

How to cut a public marketplace release of **grok-skills**. Hosts install from
git (`sfourdrinier/grok-skills`); a tagged GitHub release is the version users
pick up after they refresh marketplaces.

## When to release

- User-visible behavior, install/UX, security, or packaging change that should
  ship beyond `main` experiments.
- Prefer **semver** on the plugin version (`MAJOR.MINOR.PATCH`):
  - **PATCH** — fix/UX (e.g. review drift notes)
  - **MINOR** — new skills/agents/modes, non-breaking
  - **MAJOR** — breaking skill/CLI/envelope contracts

Optional last-validated CLI stamp updates (`accepted-version.json`, advisory
only) follow [CONTRIBUTING.md](../CONTRIBUTING.md) — never as a user allowlist.

## Checklist (every release)

### 1. Code and docs ready

- [ ] Behavior complete; dual-host parity (Claude + Codex) considered
- [ ] Docs match code: `README.md`, skill `SKILL.md`s, `plugin/references/**`,
      relevant `docs/**` (AGENTS.md rule #1)
- [ ] `CHANGELOG.md`: new `## [X.Y.Z] - YYYY-MM-DD` section (not only Unreleased)
- [ ] No secrets, no personal absolute paths in the tree

### 2. Bump version in all packaging surfaces

Keep these **identical** to `X.Y.Z`:

| File | Field |
|------|--------|
| `plugin/.claude-plugin/plugin.json` | `version` |
| `plugin/.codex-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `metadata.version` **and** `plugins[].version` |

`.agents/plugins/marketplace.json` has no version field (local path source);
no bump required unless you add one later.

### 3. Verify

```bash
# Wrapper
cd plugin/wrapper/scripts && python3 -m unittest discover -s tests -q

# Plugin companion / hooks
cd plugin/scripts && node --test tests/*.test.mjs

# Packaging (if claude CLI available)
claude plugin validate ./plugin --strict
```

Optional: install smoke from a clean cache or
`CLAUDE_PLUGIN_ROOT=$PWD/plugin node …/grok-companion.mjs preflight`.

Smoke (1.3.0+): after a live run, `/grok:status --run-id <id>` while in-flight
should report `running` (exit 0); after completion, `success` or `failure`
(exit 1 for failed targets still yields a well-formed status envelope). Status
must not modify the run directory.

Smoke (1.4.0+): `review --isolated` on a dirty tracked file runs under
`state…/worktrees/review/<runId>` and leaves no worktree after success; without
`--isolated`, cwd stays the live checkout. Direct mode + `--isolated` fails closed.

Smoke (1.5.0+): `setup --notification-mode auto`, then a background-style live
run with `GROK_COMPANION_EXECUTION_CONTEXT=background` may write
`runs/<runId>/notified.json` (`state: completed`) after the run; a second
completion path does not re-fire (`already-attempted`). With mode `off`, no
marker. Status/jobs alone never create `notified.json`.

### 4. Commit

```bash
git status   # only intended files
git add …
git commit -m "Short summary (vX.Y.Z)"
```

Prefer one release commit (or a short stack already on `main`). Working tree
clean after commit.

### 5. Tag and push

```bash
git tag -a "vX.Y.Z" -m "vX.Y.Z: one-line summary"
git push origin main
git push origin "vX.Y.Z"
```

Use an **annotated** tag matching `v` + the plugin version. Do not move or
force-push tags that others may have installed.

### 6. GitHub Release

```bash
gh release create "vX.Y.Z" \
  --title "vX.Y.Z — short title" \
  --notes-file - <<'EOF'
## Highlights
- …

## Upgrade
Claude: marketplace update / reinstall grok@grok-skills, reload plugins.
Codex: refresh marketplace, reinstall if needed, new session (SessionStart
syncs managed agents under ~/.codex/agents/).
EOF
```

Paste CHANGELOG bullets if easier; keep upgrade steps for both hosts.

### 7. Post-release smoke (maintainer machine)

- [ ] Release page exists: `https://github.com/sfourdrinier/grok-skills/releases/tag/vX.Y.Z`
- [ ] Refresh Claude and/or Codex marketplace; confirm installed plugin version
      is `X.Y.Z`
- [ ] Codex: new session → `~/.codex/agents/grok-*.toml` present with
      `# managed-by: grok-skills`, `# agent-run:`, and `GROK_AGENT_RUN` under the
      **new** cache path (`…/agents/run.mjs`)
- [ ] Optional: `/grok:preflight` or setup skill once
- [ ] Confirm skills/agents still say never invent cache paths
  ([plugin/references/plugin-root.md](../plugin/references/plugin-root.md))
- [ ] Confirm docs do **not** claim a hard Grok CLI version pin (any working CLI)

## What not to do

- Do not tag without bumping the three version fields above (hosts show stale
  numbers).
- Do not reintroduce exact-match fail-closed on `accepted-version.json`.
  Optional stamp updates after a probe suite are fine (`enforcement: none`).
- Do not delete or rewrite published tags to “fix” a bad release; cut `X.Y.Z+1`.
- Do not require a manual `/grok:setup` for Codex agents in release notes as if
  it were mandatory (SessionStart auto-installs; setup is optional).

## Quick copy-paste (after versions + CHANGELOG + tests)

```bash
VER=X.Y.Z
git add -A && git status
git commit -m "Release summary (v${VER})"
git tag -a "v${VER}" -m "v${VER}: summary"
git push origin main && git push origin "v${VER}"
gh release create "v${VER}" --title "v${VER} — summary" --notes "See CHANGELOG.md"
```
