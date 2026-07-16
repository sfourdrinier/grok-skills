# PR4 failure-mode matrix + DRY boundaries (Gate A)

**Date:** 2026-07-16  
**Branch:** `feat/pr4-implementation-handoff-1.6.0`  
**Authority:** design §14 / plan rev 13

## DRY single owners

| Behavior | Module / symbol |
|----------|--------------|
| Contract parse + path_in_scopes + load file | `implementation_contract.py` |
| Phase-1 patch + phase-2 manifest + `validate_implementation_handoff` | `implementation_handoff.py` |
| Ordered post-Grok steps for code | `code_handoff_finalize` in `implementation_handoff.py`, called only from `code._finalize` |
| Command evidence (sha256 + 4k tails) | `command_evidence.py` |
| Read-only handoff mode | `modes/handoff.py` + same validator |
| Companion handoff | thin passthrough like status |

## Failure-mode matrix

| Surface | Silent wrong | Fail closed | Test |
|---------|--------------|-------------|------|
| Finalization order | skip/reorder | assert order on FinalizeStage path | yes |
| Second pipeline | parallel finalizer | forbidden | review |
| Sentinel | missing/symlink | wrong-working-directory | existing + order |
| Unexpected commit | HEAD ≠ base | blocker; no reset | yes |
| Write scopes | string-prefix false friend | blocker | yes |
| Contract path | symlink/non-file | implementation-contract-invalid | yes |
| Temp index retained | left on disk | temp-index-retained | yes |
| Temp index gone after err | warn only | not retained blocker | yes |
| requiredValidation shell/cwd | escape | reject | yes |
| Original checkout dirty after validation | ready true | ready false | yes |
| Manifest before envelope crash | handoff ready alone | dual-condition false | yes |
| `/grok:handoff` spawns Grok | — | must not | yes |
| Companion >900 lines | — | extract | yes |
| Direct + contract | silent skip | fail closed | yes |
| Oversized patch | truncate | never | yes |
| Notify as ready | integrate without handoff | docs forbid | dual-host |

## ERROR_CLASS primary mapping

| Situation | Envelope class | Blocker strings |
|-----------|----------------|-----------------|
| Bad contract pre-Grok | implementation-contract-invalid | n/a |
| Scope violation | write-scope-violation | write-scope-violation |
| HEAD moved | unexpected-commit | unexpected-commit |
| Patch capture/size/secret | artifact-generation-failure | secret-material / size |
| Rehash/load fail | artifact-integrity-failure | integrity |
| No handoff artifacts | handoff-unavailable | n/a |
| Manifest ready, no success envelope | (handoff observation) | terminal-envelope-incomplete |
| Build/validation fail | validation-failure (existing) | validation-failure |

## Modules to create

- `plugin/wrapper/scripts/groklib/implementation_contract.py`
- `plugin/wrapper/scripts/groklib/implementation_handoff.py`
- `plugin/wrapper/scripts/groklib/command_evidence.py`
- `plugin/wrapper/scripts/groklib/modes/handoff.py`
- `plugin/skills/handoff/`
- `plugin/references/implementation-handoff.md`
