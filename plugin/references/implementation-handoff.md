# Implementation handoff (1.6.0+)

Authority: design §14. Schema is validated only by
`validate_implementation_handoff` in the wrapper (no second public JSON Schema
file).

## Trust model

Contract content is **operator-trusted**. Value of
`validation.sources.contractRequiredValidation.trustModel`:

```text
operator-contract-trusted-no-os-sandbox
```

Contract `requiredValidation` runs with `shell=False` and cwd confined to the
worktree. There is **no** OS filesystem sandbox claim for those argv.

## Contract load-time caps (fail closed before Grok)

Validated by `validate_contract` /
`plugin/wrapper/scripts/groklib/implementation_contract.py` at load
(`implementation-contract-invalid`). Do not restate elsewhere; COMPATIBILITY
summarizes migration.

| Cap / rule | Value |
|------------|-------|
| `schemaVersion` | **1** only |
| `objective` | max **2000** chars (`OBJECTIVE_MAX_CHARS`) |
| `acceptanceCriteria` | max **32** items; each max **500** chars after strip |
| `writeScopes` | required non-empty when a contract is present |
| `requiredValidation` | optional; present non-array fails closed; argv tokens must not contain NUL |
| Compatibility | optional display fields normalize empty; oversized fields **refuse** (no truncation); only schemaVersion 1 is accepted |

## Artifact layout

Under the C2 state root for a code run:

```text
runs/<runId>/
  envelope.json
  implementation-handoff.json
  artifacts/
    implementation.patch
```

## Manifest fields (schemaVersion 1)

| Field | Notes |
|-------|--------|
| `runId` | Matches code run |
| `taskId` | From contract or `no-contract` |
| `contractSha256` | Hash of normalized contract or null |
| `contractSummary` | Display metadata or null: `{taskId, objective, acceptanceCriteria[]}` - not part of readiness |
| `baseRevision` | Full SHA worktree was created from |
| `resultTreeOid` | Tree OID after staging changes |
| `changedFiles[]` | `{path, status, oldPath}` |
| `patch` | `format=git-binary-full-index-v1`, relativePath, sha256, bytes |
| `validation` | See sources authority below |
| `integration.ready` | Manifest write-time ready (not dual-condition) |
| `integration.blockers[]` | `{kind, message, detail?}` |
| `worktree` | retained path + branch |
| `createdAtUtc` | ISO-Z |

### validation.sources

- `wrapperBuildGate.authoritative: true`
- `contractRequiredValidation.authoritative: true` + trustModel string above
- `modelClaimedCommands.authoritative: false` (ignored for readiness)

## Dual-condition ready (what parents must use)

`/grok:handoff` reports ready only when:

1. Manifest loads and validates (ready=true requires non-empty `changedFiles`,
   empty blockers, validation flags true, and `patch.bytes > 0`)
2. `integration.ready === true` on the manifest
3. Completed **success** terminal envelope for same `runId` with `mode: "code"`
4. Envelope `baseRevision` is non-empty and equals the manifest base
5. Patch file exists, size matches `patch.bytes` (> 0), and sha256 re-hash matches
6. Manifest `changedFiles` path set equals paths derived from patch
   `diff --git` headers; when the envelope lists `changedFiles`, those
   destination paths must match the manifest destinations

Git-reported `changedFiles` paths keep colons and backslashes as filename
characters; only operator-supplied contract paths reject Windows drive forms.

### Path inventory and quoted patch headers (2.0 honesty)

- Wrapper path listings that feed dirty-overlap, escape checks, and handoff
  inventories use a shared **NUL-safe** `-z` inventory (`path_inventory`) so
  default `core.quotePath` C-quoting of non-ASCII names cannot invent phantom
  keys or miss a real rewrite. Bytes decode with `utf-8` + `surrogateescape`
  so non-UTF-8 filename bytes stay recoverable.
- Handoff path cross-check decodes C-quoted `diff --git` headers via
  `git_path_quote` (octal + named escapes, a/b sides, `/dev/null`). That decoder
  is **not** applied to already-raw `-z` inventory values (do not merge NUL-safe
  `-z` decoding with C-quote decoding).
- Companion dirty-guard touch set (`loadPatchTouchPaths`) unions numstat with
  `diff --git` / rename-copy headers (both sides). Non-empty numstat makes
  headers load-bearing (`blocked-patch-headers` when empty/unparseable or
  uncorroborated). Pure renames include **both** old and new paths in the
  dirty-overlap set. Same C-style unquote rules (`unquoteGitPath`). Shared
  golden vectors: [git-c-quoted-path-vectors.json](git-c-quoted-path-vectors.json)
  (Python + Node unit parity; no runtime cross-language dependency).
- Patch generation also fails closed on secret-shaped material **and** exact
  injected-credential denylist occurrence in patch bytes.
- Auto/peer apply-time patch integrity recheck is best-effort under trusted
  local state (manifest bytes/size/hash via `verifyPatchAgainstManifest`); it
  is not an atomic TOCTOU guarantee against hostile concurrent substitution.
  Post-stat hash/read failures return structured `{ok:false, reason:"patch unreadable"}`
  so auto/peer finalize `patch-integrity-failure` (or pre-hash
  `blocked-patch-unreadable`) instead of throwing.
  Canonical under-lock ladder (exclusive lock, durable marker, heal under
  revalidate, `marker-persist-failure`): [integration-modes.md](integration-modes.md)
  Shared apply spine.

## Parent apply checklist

Mode-aware integrate (canonical:
[integration-modes.md](integration-modes.md)):

- **code direct:** source edits already live in the operator tree; protected
  paths rolled back if touched. No patch gate required for the edit to exist.
- **code auto:** companion may auto-apply after dual-condition ready +
  apply-time revalidation (patch integrity recheck + shared dirty-guard apply
  spine - see [integration-modes.md](integration-modes.md)). Use this checklist
  only if apply did not run or failed.
- **code review:** never auto-applies - use the checklist below.
- **ACP peer:** always external worktree during the session; at ready
  `peer stop`, `direct`/`auto` apply via the same spine (direct needs consent),
  `review` retains. `/grok:handoff` refuses peer runIds (code-mode only).

### Manual apply (review / when auto did not apply)

1. `handoff --run-id` success + ready
2. Confirm base still present / ancestry
3. Dirty overlap inventory on target paths (`git status --porcelain -z`)
4. Explicit patch integrity recheck: on-disk patch bytes/size/sha still match the
   handoff manifest (same integrity gate auto/peer re-run before apply)
5. Refuse if the patch touch set includes a protected path (same deny-write SSOT
   auto/peer use: [deny-write-globs.json](deny-write-globs.json) - `.env` /
   keys / `.git/**` / credentials, including rename source or destination)
6. `git apply --check --binary path/to/implementation.patch`
7. Explicit apply only with operator intent
8. Re-run project validation on parent
9. Record runId + patch sha256

**Never** auto-commit, merge, cherry-pick, or push from this plugin in any mode.

## transfer vs result vs handoff

| Surface | Purpose |
|---------|---------|
| transfer | Conversation / session context |
| result | Companion job stdout by job id |
| handoff | Verified implementation by **runId** |

## Notify vs handoff

Notifications (1.5.0) are optional **signals** that a terminal attempt finished.
They are not proof of integration readiness. Always call handoff before integrate.

## Known limitation: secret-shaped fixture changes (2.0.0+)

The phase-1 patch is scanned for secret-shaped material and REFUSED fail-closed
(`artifact-generation-failure`) when it matches - even when the "secrets" are
synthetic fixtures, e.g. moving or editing this repo's own redaction tests.
Such changes cannot produce a handoff patch artifact by design. Integrate them
from the retained worktree instead: verify the work in the worktree (test
counts, suite, caps), generate the diff yourself (`git -C <worktree> add -A &&
git -C <worktree> diff --cached --binary`), and apply with operator intent.
Live evidence: docs/checklists/2.0-live-smoke-ledger.md (Task 0.6, cycle 4).

## Cleanup semantics for iteration chains

`code --continue-run` keeps one shared external worktree: the seed owns the
sibling marker and directory name; each continuation's `run.json` records the
same `worktreePath` plus `continuesRunId`. Cleanup of a continuation therefore
removes only that run's directory (stored envelope, artifacts, session archive)
and leaves the shared worktree for its owner, with a success note naming the
owner run - not a `state-ownership-violation`. Cleanup of the seed removes the
worktree as usual even when continuation run dirs still reference it; later
continuation cleanups treat a missing worktree as a note and still reap their
run dirs. A non-continuation run whose record points at a foreign worktree
still fails closed.
