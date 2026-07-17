<!-- plugin/references/integration-modes.md -->

# Integration modes (how edits land)

**Canonical reference.** Skills, agents, README, and handoff docs link here
instead of restating the matrix. Orthogonal to **run mode** (security posture);
see [Naming: two "direct" axes](#naming-two-direct-axes) below.

Workspace default for code / implement / peer is **`direct`**. First direct run
in a target repo without recorded consent fails closed with a trust summary;
accept once via `setup --integration direct` (optionally `--target <repo>`).

```bash
node "$SKILL_BASE/run.mjs" setup --integration direct
# isolation without apply:
node "$SKILL_BASE/run.mjs" setup --integration auto    # or review / worktree
# per-run override:
node "$SKILL_BASE/run.mjs" code --integration review --target '...' --base 'HEAD' ...
```

`userConfig.integrationMode` / `CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE` is a
**default hint only** - it does not satisfy consent. Only setup does.

## The three product modes

| Mode | Isolation | How changes land | When to use |
|------|-----------|------------------|-------------|
| **`direct` (DEFAULT)** | **None** - no external worktree | Grok edits **your real working tree** live (hardened-direct under runMode hardened) | Trusted-input peer implementer (same class as a host subagent editing the checkout) |
| **`auto`** | External git worktree + full dual-condition verification | On dual-condition **ready**, companion **auto-applies** the verified patch to your tree (apply-time revalidation; never half-applies) | Want isolation + verification, then automatic land |
| **`review`** | External git worktree + patch + handoff manifest | **Never** auto-applies; parent applies manually after ready handoff | Untrusted / review-first changes |

Companion also accepts **`worktree`** as an isolation alias (wrapper path =
isolated worktree; parent apply stays manual, same family as review). Prefer
`review` or `auto` in product docs.

### `direct` (default) - hardened-direct

Under **runMode hardened** (the security default), `integration=direct` is
**hardened-direct**:

- **Private auth home** per run (credentials isolated; not your normal `~/.grok`
  session store for the child)
- **OS sandbox** write-confined to the **repo root** (+ private tmp)
- **Secret redaction** on the single stdout envelope
- **Protected paths** (`.git/**`, `.env` / `.env.*`, keys, hooks): deny-scan +
  **post-run rollback** if touched (best-effort; not seatbelt subpath deny)
- **Source edits land live** - no worktree isolation, no pre-apply dual-condition
  gate, no forensic patch required for the edit to exist
- **One-time setup consent** per target repo before the first direct run

Dirty-tree policy: refuse when a changed path **overlaps** operator-dirty paths
captured at run start, unless `--force`. Clean-elsewhere is fine.

### `auto`

1. Code (or peer) runs in an **isolated external worktree**
2. Full dual-condition verification (ready manifest + success envelope + patch
   rehash - same authority as `/grok:handoff`)
3. On ready: companion revalidates at apply time (`git apply --check --binary`,
   then apply). Any failure **stops** and reports partial/blocked honestly
4. Not-ready: nothing applied; blockers surface on stdout

### `review`

Current 2.0 isolation path without apply: worktree + immutable patch + handoff
manifest. Parent integrate protocol is manual (see
[implementation-handoff.md](implementation-handoff.md)). The plugin does not
commit, merge, cherry-pick, or push in any mode.

## Honesty (trusted-input posture)

**`direct` is the trusted-input default.** It is not a multi-tenant sandbox and
not isolation theater.

| Claim | Reality |
|-------|---------|
| Sandbox protects the tree | **No.** Sandbox confines writes to the **repo root** but does **not** prevent writes to `.git` / `.env` / keys / hooks **inside** it. Deny-scan + rollback are **best-effort post-run** layers. |
| Private home protects your checkout | **No.** Private home isolates **Grok auth/config**, not your working tree. |
| Grok cannot read secrets | **No.** Documented **D-SECRETREAD** gap: write confinement is not a read firewall. Absolute-path reads remain possible. |
| Edits can be rolled back | **Only protected paths** (best-effort). Ordinary source edits land live; your git history / stash is the recovery story. |
| Silent default flip | **No.** First direct run without setup consent fails closed with the accept command. |

Choose **`auto`** or **`review`** when you want isolation and a verified patch
before anything lands on the operator tree.

Full security notes: [SECURITY.md](../../SECURITY.md),
[docs/OPEN-SECURITY-DECISIONS.md](../../docs/OPEN-SECURITY-DECISIONS.md).

## Naming: two "direct" axes

Both axes use the word **direct**. They are **orthogonal**.

| Axis | Values | Meaning |
|------|--------|---------|
| **runMode** (security) | `hardened` (default) \| `direct` | **hardened:** private home + sandbox verification + redaction. **direct:** installed Grok CLI + normal `~/.grok` auth; less isolation; no verified handoff artifacts. Set via `setup --run-mode` or `GROK_SKILLS_MODE`. |
| **integration** (how edits land) | `direct` (default) \| `auto` \| `review` (`worktree` alias) | **direct:** live tree (hardened-direct when runMode is hardened). **auto:** worktree then apply-on-ready. **review:** worktree + manual parent apply. Set via `setup --integration` or `--integration` on the run. |

Examples:

- `runMode=hardened` + `integration=direct` → **hardened-direct** (default product
  path): private home + sandbox-to-repo + live source edits + consent.
- `runMode=hardened` + `integration=review` → isolated worktree, manual apply.
- `runMode=direct` + any integration that needs handoff artifacts → handoff /
  implement / contract path **refuse** fail-closed (no isolation evidence to
  attest). Prefer hardened runMode for auto/review handoff.

When docs say "direct mode" without a qualifier, prefer **integration=direct**
for edit-landing, and **runMode=direct** only when discussing installed-CLI
security posture.

## ACP peer channel

The **default** implementation path for `grok-engineer-coder` is the live
multi-turn ACP peer (`peer start` / `prompt` / `stop`). One-shot `code` is the
fallback (`GROK_DISABLE_ACP=1` or peer unavailable).

Peer results integrate through the **same** integration modes:

| Mode after peer-stop / ready | Effect |
|------------------------------|--------|
| `direct` | Edits already live in the tree (or apply path when applicable) |
| `auto` | Apply verified patch on ready (revalidated) |
| `review` | Patch + manifest only; parent applies manually |

`peer stop` runs contract `requiredValidation` and the build gate **for real**;
`integration.ready=true` only from non-forgeable evidence. Peer-stop then applies
its verified patch **itself** per the active integration mode (above);
`/grok:handoff --run-id` stays **code-mode only** and refuses peer runIds
(`handoff-unavailable`), so peer integration never routes through it.

## Mode-aware integrate rules (summary)

| Mode | Auto-apply? | Parent apply? |
|------|-------------|---------------|
| `direct` | N/A - edits already live (source); protected-path rollback only | Optional review of live diff; no patch gate required for the edit to exist |
| `auto` | **Yes**, only after dual-condition ready + apply-time revalidation | Only if auto apply failed or was not used |
| `review` / `worktree` | **No** | **Yes** - manual after ready handoff (`git apply --check --binary`, then explicit apply) |

**Never** auto-commit, merge, cherry-pick, or push from this plugin in any mode.

Handoff skill remains **read-only** (never applies). `implement` is **verify-only**
(code + handoff; does not apply) - for apply-on-ready use `code` / peer with
`integration=auto`.

## Related

- [implementation-handoff.md](implementation-handoff.md) - dual-condition ready + parent apply checklist (review / manual path)
- [execution-context.md](execution-context.md) - foreground vs background notify context
- Root [README.md](../../README.md) - install + skill table
- [SECURITY.md](../../SECURITY.md) - trusted-input + direct-default posture
