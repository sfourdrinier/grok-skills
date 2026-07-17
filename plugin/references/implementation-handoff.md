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

## Parent apply checklist

1. `handoff --run-id` success + ready  
2. Confirm base still present / ancestry  
3. Dirty overlap check on target paths  
4. `git apply --check --binary path/to/implementation.patch`  
5. Explicit apply only with operator intent  
6. Re-run project validation on parent  
7. Record runId + patch sha256  

**Never** auto-apply, auto-commit, merge, cherry-pick, or push from this plugin.

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
