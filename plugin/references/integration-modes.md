<!-- plugin/references/integration-modes.md -->

# Integration modes (how edits land)

**Canonical reference.** Skills, agents, README, and handoff docs link here
instead of restating the matrix. Orthogonal to **run mode** (security posture);
see [Naming: two "direct" axes](#naming-two-direct-axes) below.

Workspace default for **code** and **peer-stop landing** is **`direct`**.
First direct landing in a target repo without recorded consent fails closed
with a trust summary (one-shot code run, or ready peer-stop apply); accept
once via `setup --integration direct` (optionally `--target <repo>`), or opt
into isolation with `setup --integration auto|review` before promising
live-tree success. **`implement` is different:** it always forces an isolated
worktree + verify-only handoff and never lands on the live tree, even when
the workspace default is direct/auto. For ACP peer, `direct` still means
stop-time apply after an always-external worktree - not live-edit during
prompts.

```bash
node "$SKILL_BASE/run.mjs" setup --integration direct
# isolation without apply:
node "$SKILL_BASE/run.mjs" setup --integration auto    # or review / worktree
# per-run override:
node "$SKILL_BASE/run.mjs" code --integration review --target '...' --base 'HEAD' ...
```

`userConfig.integrationMode` / `CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE` is a
**default hint only** - it does not satisfy consent. Only setup does.

## The three product modes (one-shot `code`)

The matrix below is the **one-shot `code`** landing story (not
`/grok:implement`, which always forces worktree + verify-only - see
[implement always forces worktree](#implement-always-forces-worktree--verify-only)).
ACP **peer** always uses an external retained worktree during the session and
lands only at **peer-stop** - see [ACP peer channel](#acp-peer-channel). Do not
read peer `integration=direct` as live-edit direct.

| Mode | Isolation (one-shot code) | How changes land (one-shot code) | When to use |
|------|---------------------------|----------------------------------|-------------|
| **`direct` (DEFAULT)** | **None** - no external worktree | Grok edits **your real working tree** live (hardened-direct under runMode hardened) | Trusted-input one-shot implementer (same class as a host subagent editing the checkout) |
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
- **Protected paths**: deny-scan + **post-run rollback** if touched (best-effort;
  not seatbelt subpath deny). Covered: `.env` / `.env.*`, keys / `.netrc` /
  `.npmrc` / `.envrc`, and the sensitive `.git` subset `config`, `HEAD`,
  `packed-refs`, `hooks/**`, `refs/**` (a moved/created ref is reverted/removed).
  **NOT covered:** `.git/index`, `.git/COMMIT_EDITMSG`, and loose `.git/objects`
  (benign working state git rewrites on ordinary reads; loose objects are inert
  until a guarded ref points at them)
- **Source edits land live** - no worktree isolation, no pre-apply dual-condition
  gate, no forensic patch required for the edit to exist
- **One-time setup consent** per target repo before the first direct run

Dirty-tree policy: refuse when a changed path **overlaps** operator-dirty paths
captured at run start, unless `--force`. Clean-elsewhere is fine.

### `auto`

1. Code (or peer) runs in an **isolated external worktree**
2. Full dual-condition verification (ready manifest + success envelope + patch
   rehash - same authority as `/grok:handoff`)
3. On ready: companion revalidates at apply time (manifest + patch integrity,
   then the shared apply spine below). Any failure **stops** and reports
   partial/blocked honestly
4. Not-ready or missing/unparseable code envelope: nothing applied; companion
   emits **one** complete failure envelope (`mode=code`, classified error,
   `response.integration.applied=false`) - never a partial schema-only object
   and never invented success. Classification uses existing C4 classes:
   empty stdout => `output-missing`; non-JSON stdout => `output-malformed`;
   parseable code envelope without a usable runId => `handoff-unavailable`
   (not stdout corruption).

### Shared apply spine (auto + peer)

Canonical ladder for landing a verified patch on the operator tree
(`plugin/scripts/lib/integrate.mjs` +
`plugin/scripts/lib/integrate-apply-state.mjs`). Auto and ready peer-stop share
it; consent / readiness / target identity stay outside the helper. This is
**not** an atomic TOCTOU seal against a hostile concurrent writer of the patch
or tree.

#### Exclusive apply lock + durable marker

Per `(runId, targetKey)`:

1. **Lock** - atomic mkdir
   `runs/<id>/apply-locks/<targetKey>.lock` plus a durable `owner.json`
   (`pid`, `startToken`, `acquiredAt`). Owner write failure removes the lock
   dir and fails closed (never leaves an ownerless lock that could be
   age-stolen).
2. **Reclaim** - only a **positively dead** owner (dead pid or pid-reuse via
   mismatched startToken) after a short settle may be reclaimed, and reclaim is
   **owner-atomic**: observe the owner identity, rename the lock dir to a private
   tombstone, recheck the moved identity, then delete only that tombstone.
   If another process replaced the lock between observe and rename, the recheck
   fails and the replacement is restored (never deleted). **Ownerless /
   unknown / unreadable** locks are **never** age-reclaimed - acquire waits or
   times out (manual cleanup if a lock is abandoned without a durable owner).
3. **Marker** - durable `integration-applied-<targetKey>.json` keyed by
   `patchSha` + `targetKey` (tmp + rename, then re-read to prove presence).

#### Under-lock ladder (`completeIntegrationApplyUnderLock`)

1. Matching marker + reverse-check still applies => **`already-applied`**
   (idempotent restop).
2. Marker present but tree no longer has the patch (operator reverted) => clear
   marker and re-apply.
3. No marker but reverse-check says the tree already has the patch
   (crash-after-apply residue) => **revalidate under lock**, then heal the
   marker; heal failure => `marker-persist-failure` (tree stays applied; not
   claimed as durable applied success without a marker).
4. Else revalidate under lock, run the dirty-guard apply spine, then
   `finalizeAppliedWithMarker`. If marker write fails after a successful apply,
   reverse the apply when possible (`marker-persist-failure`); reverse failure
   => `manual-needed`.

#### Dirty-guard apply spine (`applyPatchWithGuards`)

1. **Patch integrity** (caller, before/under lock revalidation) - best-effort
   recheck of on-disk `implementation.patch` bytes/size/hash against the
   revalidated handoff manifest (`verifyPatchAgainstManifest`) under trusted
   local state; mismatch => `patch-integrity-failure` (fail closed).
2. **Dirty status** - `git status --porcelain -z --untracked-files=all`
   (NUL-safe). Non-zero / truncated status => `blocked-dirty-status` (no blind
   apply).
3. **Patch path list** - `git apply --numstat --binary` with C-style path
   unquote for default `core.quotePath` non-ASCII names (Node `unquoteGitPath`;
   golden vectors in
   [git-c-quoted-path-vectors.json](git-c-quoted-path-vectors.json)); numstat
   failure => `blocked-numstat`.
4. **Header union (`loadPatchTouchPaths`)** - union numstat destinations with
   `diff --git` / rename-copy headers (both old and new sides). Non-empty
   numstat makes headers **load-bearing**: empty/unparseable headers or a
   numstat path not corroborated by headers => **`blocked-patch-headers`**
   (no numstat-only fallback). Pure renames put **both** old and new paths in
   the touch set (dirty-overlap + protected-path) so a dirty or protected
   source/destination cannot fail open.
5. **Protected-path pre-block** - any touch-set path matching the shared
   deny-write globs ([deny-write-globs.json](deny-write-globs.json); same list
   Python direct-mode finalize uses) => `blocked-protected-path` **before**
   `git apply --check` / apply. Tree unchanged. Pre-apply refuse only - not
   direct-mode snapshot/rollback and not a `protected-path-write` class.
6. **Dirty overlap** - any touch path already dirty in the operator checkout =>
   `blocked-dirty-overlap` (operator commits/stashes, then re-runs).
7. **`git apply --check --binary`** then **`git apply --binary`**; on apply
   failure reverse with `git apply -R` when possible (`rolled-back` /
   `manual-needed`).

Published outcomes include: `already-applied`, `applied`,
`blocked-dirty-status`, `blocked-numstat`, `blocked-patch-headers`,
`blocked-protected-path`, `blocked-dirty-overlap`, `blocked-apply-check`,
`rolled-back`, `marker-persist-failure`, `manual-needed`, plus caller-owned
readiness / consent / integrity failures.

### `implement` always forces worktree + verify-only

`/grok:implement` is **not** product-direct land. The companion gate always
rewrites implement to `--integration worktree` (never live direct; never
apply-on-ready). It runs code + handoff only; exit 0 only when dual-condition
ready. Product `direct` default still applies to **code** and **peer-stop**
landing. For apply-on-ready use `code --integration auto` (or peer-stop with
`auto` / consented `direct`).

### `continue-run` (one-shot code)

- **Forbidden on continue:** `--target`, `--base`, `--contract-file` (usage-error).
  Target identity, base, and prior contract are derived from the prior run.
- **Target workspace** for consent / apply is the prior run's durable
  `run.json` `targetWorkspace` / `repository` (not companion cwd). Missing
  prior metadata falls back to cwd-scoped default.
- **Direct consent exempt** on continue (wrapper reuses retained worktree
  lineage). Effective mode still resolves: `auto` keeps apply-on-ready on the
  **new** run; `review` retains (manual parent apply); product `direct` maps
  the wrapper to worktree lineage without live apply.
- Direct continue uses the **hardened wrapper** for retained lineage (never
  `runDirectGrok` live-edit). Handoff the **new** run id before integrate.

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
| **integration** (how edits land) | `direct` (default) \| `auto` \| `review` (`worktree` alias) | **One-shot code:** direct = live tree (hardened-direct when runMode is hardened); auto = worktree then apply-on-ready; review = worktree + manual parent apply. **ACP peer:** always external worktree during the session; at peer-stop, direct/auto apply the verified ready patch (direct needs consent), review retains. Set via `setup --integration` or `--integration` on the run / peer-stop. |

Examples:

- `runMode=hardened` + `integration=direct` + one-shot **code** → **hardened-direct**:
  private home + sandbox-to-repo + live source edits + consent.
- `runMode=hardened` + `integration=direct` + **ACP peer** → external worktree for
  the whole session; at ready peer-stop, consented apply of the verified patch
  (not live-edit during prompts).
- `runMode=hardened` + `integration=review` → isolated worktree, manual apply
  (code handoff or peer-stop retain).
- `runMode=direct` + any integration that needs handoff artifacts → handoff /
  implement / contract / peer path **refuse** fail-closed (no isolation evidence to
  attest; peer is hardened-only). Prefer hardened runMode for auto/review handoff.

When docs say "direct mode" without a qualifier, prefer **integration=direct**
for edit-landing, and **runMode=direct** only when discussing installed-CLI
security posture. When they say peer direct, mean **stop-time apply**, never
code-style live-tree editing.

## ACP peer channel

The **default** implementation path for `grok-engineer-coder` is the live
multi-turn ACP peer (`peer start` / `prompt` / `stop`). One-shot `code` is the
fallback (`GROK_DISABLE_ACP=1` or peer unavailable). Peer is **hardened runMode
only**.

### Peer isolation is always external

Every peer session creates a **private home + external retained worktree**.
Prompt-time source edits land in that worktree only. The operator checkout is
untouched until a ready **peer-stop** apply (if the mode applies). Peer
`integration=direct` is therefore **not** one-shot code live-edit direct.

### Peer-stop landing matrix

| Mode at peer-stop | Session isolation | On evidence-backed ready | Consent |
|-------------------|-------------------|--------------------------|---------|
| **`direct`** | Always external worktree | Companion **applies** the verified ready patch to the target checkout (shared apply spine) | **Required** (`setup --integration direct` for that repo) |
| **`auto`** | Always external worktree | Companion **applies** the verified ready patch (same spine; no separate code-style live path) | Not required for apply |
| **`review`** / **`worktree`** | Always external worktree | Patch + handoff manifest **retained**; parent applies manually | N/A |

`peer stop` runs contract `requiredValidation` and the build gate **for real**;
`integration.ready=true` only from non-forgeable evidence. Peer-stop then applies
or retains **itself** per the matrix above; `/grok:handoff --run-id` stays
**code-mode only** and refuses peer runIds (`handoff-unavailable`), so peer
integration never routes through it.

Companion completion honesty (parity with `code --integration auto`): peer-stop
captures the wrapper envelope, runs integration, and attaches the final apply
outcome (`response.integration.applied` / `outcome`) via the shared final-envelope
helper under rewrite-before-write/store/finalize: onStdout computes final
emitStdout/effectiveCode **before** first write; then stdout write; then
storeJobStdout (same envelope for `/grok:result`); then updateJob/finalize from
final envelope + effective exit code; then notify. Emits **exactly one** final
stdout envelope. A blocked apply (consent, dirty-overlap, integrity, etc.) is
`status: failure`, nonzero exit, job failed, target untouched - never a raw
wrapper `success` for an unapplied ready peer-stop. **Peer-stop is not
completion-notification eligible** (`NOTIFY_ELIGIBLE_MODES` is
review/reason/code/verify/adversarial-review only); do not expect a toast for
peer sessions even when notifications are on.

## Mode-aware integrate rules (summary)

One-shot **code** (live vs worktree during the run):

| Mode | Auto-apply? | Parent apply? |
|------|-------------|---------------|
| `direct` | N/A - edits already live (source); protected-path rollback only | Optional review of live diff; no patch gate required for the edit to exist |
| `auto` | **Yes**, only after dual-condition ready + apply-time revalidation | Only if auto apply failed or was not used |
| `review` / `worktree` | **No** | **Yes** - manual after ready handoff (`git apply --check --binary`, then explicit apply) |

**ACP peer** always isolates in an external worktree; at stop, `direct` and
`auto` both apply the verified ready patch (direct needs consent), while
`review`/`worktree` retain for manual parent apply - see the peer-stop matrix
above. Do not summarize peer as "direct = edits already live".

**Never** auto-commit, merge, cherry-pick, or push from this plugin in any mode.

Handoff skill remains **read-only** (never applies). `implement` is **verify-only**
and always isolated (code + handoff; never applies, never live lands) - for
apply-on-ready use `code --integration auto` or peer-stop with
`integration=auto` / consented `direct`. Peer-stop remains **outside**
completion-notification eligibility.

## Related

- [implementation-handoff.md](implementation-handoff.md) - dual-condition ready + parent apply checklist (review / manual path)
- [execution-context.md](execution-context.md) - foreground vs background notify context
- Root [README.md](../../README.md) - install + skill table
- [SECURITY.md](../../SECURITY.md) - trusted-input + direct-default posture
