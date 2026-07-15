# wrapper/scripts/groklib/modes/verify.py
#
# `verify` mode (spec 5.5): read-only verification of an EXISTING external
# worktree that a prior `code` run produced. The worktree path (--worktree) must
# already appear in `git worktree list --porcelain` and carry a valid sibling
# ownership marker before Grok is ever spawned. Grok gets the read-only tool set
# plus a terminal for the approved verification commands (builds, type checks,
# tests) -- NO source-editing tools -- and is hermetic (no --web). After the
# run, any change that git does NOT genuinely ignore fails the run closed as
# unexpected-edits. Build/test/cache output is tolerated ONLY when git actually
# ignores it (check-ignore), at ANY depth, so a multi-package workspace build under
# packages/<pkg>/dist does not false-fail while a TRACKED file merely sitting
# under a build/dist-named directory is still flagged (PR968 codex
# verify-artifact-ignore). The envelope carries a `verifier` identity plus the
# verdict extracted from the wrapper-owned verdict schema; a missing or invalid
# verdict is verifier-unavailable. The shared worktree lifecycle
# (_worktree.run_worktree_mode) owns the private-home isolation, execution,
# model/sandbox verification, and single-teardown cleanup; verify never owns the
# worktree it inspects, so it leaves the worktree untouched.

import argparse
import json
import pathlib
from typing import Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import worktree as worktree_mod
from groklib import worktree_escape
from groklib.modes import _shared
from groklib.modes._worktree import (
    FinalizeStage,
    WorktreePrep,
    WorktreeStage,
    run_worktree_mode,
)

# Read-only inspection tools plus the terminal for approved verification
# commands (Task 0 inventory). No search_replace/write: verify never edits.
_TOOLS: Tuple[str, ...] = ("read_file", "grep", "list_dir", "run_terminal_command")

_VERDICT_VALUES: Tuple[str, ...] = ("pass", "fail", "inconclusive")

# The wrapper-owned verdict schema verify ALWAYS constrains its result to. It is
# embedded in the prompt (the wrapper owns and enforces it) and the wrapper
# validates the returned structured output against it in finalize; a missing or
# schema-violating verdict is classified verifier-unavailable rather than a bare
# schema-mismatch, because for verify an unusable verdict IS an unavailable
# verifier.
_VERDICT_SCHEMA: dict = {
    "type": "object",
    "required": ["verdict", "evidence"],
    "properties": {
        "verdict": {"type": "string", "enum": list(_VERDICT_VALUES)},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def _log(function: str, message: str) -> None:
    log_stderr("modes.verify", function, message)


def _adopt_worktree(worktree_relative: str) -> worktree_mod.ExternalWorktree:
    """Adopt the EXISTING --worktree: prove registration + ownership, then build the ExternalWorktree.

    Fails closed as invalid-target for a missing/non-directory path,
    worktree-failure when the path is not a registered git worktree, and
    state-ownership-violation when the sibling ownership marker is absent,
    malformed, or names a different run than the worktree directory. The main
    worktree (listed first by git) supplies the repository root, and the
    worktree's current HEAD is the confinement base for the post-run change scan.
    """
    if not isinstance(worktree_relative, str) or not worktree_relative.strip():
        raise GrokWrapperError(
            "invalid-target", "verify requires a non-empty --worktree path", {"worktree": worktree_relative}
        )

    worktree_path = pathlib.Path(worktree_relative).expanduser().resolve()
    if not worktree_path.is_dir():
        _log("_adopt_worktree", "worktree path is not a directory: {}".format(worktree_path))
        raise GrokWrapperError(
            "invalid-target",
            "the --worktree path does not exist or is not a directory: {}".format(worktree_relative),
            {"worktree": str(worktree_path)},
        )

    entries = worktree_mod.parse_worktree_porcelain(
        worktree_mod.git_checked(worktree_path, "worktree", "list", "--porcelain")
    )
    if not entries:
        raise GrokWrapperError(
            "worktree-failure",
            "no registered git worktrees found for {}".format(worktree_path),
            {"worktree": str(worktree_path)},
        )

    repo_root = entries[0][0]
    matched_branch: Optional[str] = None
    registered = False
    for entry_path, entry_branch in entries:
        if entry_path == worktree_path:
            registered = True
            matched_branch = entry_branch
            break
    if not registered:
        _log("_adopt_worktree", "path {} is not a registered worktree".format(worktree_path))
        raise GrokWrapperError(
            "worktree-failure",
            "the --worktree path is not a registered git worktree: {}".format(worktree_path),
            {"worktree": str(worktree_path)},
        )

    marker_path = worktree_mod.marker_path_for(worktree_path)
    owner_run_id = runstate.verify_owner_marker(marker_path)
    expected_run_id = worktree_path.name
    if owner_run_id != expected_run_id:
        _log(
            "_adopt_worktree",
            "marker run id {!r} does not match worktree {!r}".format(owner_run_id, expected_run_id),
        )
        raise GrokWrapperError(
            "state-ownership-violation",
            "ownership marker run id does not match the worktree: {}".format(worktree_path),
            {"markerRunId": owner_run_id, "worktreeRunId": expected_run_id},
        )

    head_revision = worktree_mod.git_checked(worktree_path, "rev-parse", "HEAD").strip()
    return worktree_mod.ExternalWorktree(
        path=worktree_path,
        branch=matched_branch if matched_branch is not None else "",
        base_revision=head_revision,
        repo_root=repo_root,
    )


def _build_prompt(task_text: str) -> str:
    """Assemble the verify prompt: verification directive + wrapper-owned verdict schema + task."""
    schema_json = json.dumps(_VERDICT_SCHEMA, indent=2, sort_keys=True)
    return (
        "You are verifying an existing change inside an isolated git worktree. Treat every source "
        "file as read-only: use only the read-only inspection tools and the terminal for the "
        "approved verification commands (builds, type checks, tests). When finished, return a "
        "structured result that matches EXACTLY this JSON schema:\n{schema}\n\n"
        "`verdict` MUST be one of \"pass\", \"fail\", or \"inconclusive\"; `evidence` MUST list the "
        "concrete checks you ran and their outcomes.\n\n=== VERIFICATION TASK ===\n{task}"
    ).format(schema=schema_json, task=task_text)


def _extract_verdict(structured: Optional[object], effective_model: str) -> dict:
    """Validate the structured output against the wrapper-owned verdict schema and build the verifier field.

    A missing structured object, a verdict outside the enum, or an evidence
    value that is not an array of strings is a fail-closed verifier-unavailable:
    for verify, an unusable verdict IS an unavailable verifier.
    """
    if not isinstance(structured, dict):
        _log("_extract_verdict", "grok returned no structured verdict object")
        raise GrokWrapperError(
            "verifier-unavailable",
            "the verifier returned no structured verdict",
            {"reason": "structured-output-missing"},
        )
    verdict = structured.get("verdict")
    evidence = structured.get("evidence")
    if verdict not in _VERDICT_VALUES:
        _log("_extract_verdict", "invalid verdict value {!r}".format(verdict))
        raise GrokWrapperError(
            "verifier-unavailable",
            "the verifier verdict is missing or not one of {}".format(list(_VERDICT_VALUES)),
            {"verdict": verdict if isinstance(verdict, str) else None},
        )
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        _log("_extract_verdict", "verdict evidence is not an array of strings")
        raise GrokWrapperError(
            "verifier-unavailable",
            "the verifier evidence is missing or not an array of strings",
            {"reason": "evidence-malformed"},
        )
    return {"identity": "grok-{}".format(effective_model), "verdict": verdict}


def run(args: argparse.Namespace) -> dict:
    """Adopt the existing --worktree, then drive the shared worktree lifecycle for the read-only verify run.

    The worktree is adopted (registration + ownership proven, repository root and
    confinement base resolved) BEFORE the shared lifecycle so the run's
    repository is known for the C4 envelope and the pre-Grok registration/marker
    checks fail closed exactly like an invalid --target resolution.
    """
    binary = _shared.resolve_binary(args)
    task_text = _shared.resolve_task_text(args)

    worktree = _adopt_worktree(args.worktree)
    # Change-confinement base: a full working-tree snapshot taken at verify
    # ENTRY, before Grok runs. This is the code->verify handoff fix (Task 11
    # concern 2): the adopted worktree still carries the prior code run's
    # UNCOMMITTED edits, so confining against the worktree HEAD would
    # misattribute those pre-existing edits to verify. The snapshot captures
    # them here so finalize flags only edits verify itself causes.
    entry_snapshot = worktree_mod.capture_worktree_snapshot(worktree)
    # Entry baseline of the ORIGINAL checkout's tracked working-tree divergence,
    # captured at verify entry (before Grok runs). assert_changes_within uses it
    # to flag only NEWLY diverged tracked paths, so the operator's pre-existing
    # uncommitted tracked work in the original checkout is never misattributed to
    # verify.
    original_baseline = worktree_escape.capture_original_checkout_baseline(worktree.repo_root)

    def _prepare(stage: WorktreeStage) -> WorktreePrep:
        stage.holder.worktree = worktree
        return WorktreePrep(
            worktree=worktree,
            cwd=worktree.path,
            prompt_text=_build_prompt(task_text),
            instructions=[],
            tools=_TOOLS,
        )

    def _finalize(stage: FinalizeStage) -> None:
        finalize_worktree = stage.worktree
        changed_files, diff_text = worktree_mod.diff_since_snapshot(finalize_worktree, entry_snapshot)
        stage.acc.changed_files = changed_files
        stage.acc.diff_summary = diff_text
        stage.acc.effective_working_directory = str(finalize_worktree.path)
        # PR968 codex verify-artifact-ignore: verify has NO writable roots. The
        # only post-run changes it tolerates are build/test outputs git GENUINELY
        # ignores (check-ignore), which assert_changes_within recognises at ANY
        # depth via _is_ignored_artifact. Passing an empty root set means a change
        # is tolerated ONLY when git ignores it -- never merely because it sits
        # under a build/dist-named directory, so a TRACKED build/dist source file
        # touched during verify is flagged unexpected-edits.
        worktree_escape.assert_changes_within(
            finalize_worktree,
            (),
            worktree_changed=changed_files,
            original_baseline=original_baseline,
        )
        stage.acc.verifier = _extract_verdict(stage.result.structured, stage.effective_model)
        stage.progress.safe_emit(
            "validate", "verifier verdict extracted", data={"verdict": stage.acc.verifier["verdict"]}
        )

    return run_worktree_mode(
        mode="verify",
        binary=binary,
        requested_model=args.model,
        web_access=False,
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        repository=str(worktree.repo_root),
        target_workspace=None,
        worktree_retained=False,
        prepare=_prepare,
        finalize=_finalize,
        elicit_schema=_VERDICT_SCHEMA,
    )
