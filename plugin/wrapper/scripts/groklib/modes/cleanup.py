# wrapper/scripts/groklib/modes/cleanup.py
#
# `cleanup --run-id [--confirm]` mode: verifies the run directory's ownership
# marker, rebuilds the Task 8 ExternalWorktree from the C2 run.json record when
# one is recorded, and then either reports (dry-run, no --confirm) or removes
# (--confirm) the run dir plus the external worktree and its branch. With
# --confirm, an OWNER-MARKED worktree is removed even when dirty (code mode leaves
# it dirty by design; Grok dogfood-2 #8) -- the marker plus --confirm are the
# authority. A worktree without a valid owner marker is still refused
# (state-ownership-violation), and any other worktree-failure retains everything
# and reports it. An unknown run id is `invalid-target`.
#
# Iteration chains (code --continue-run): a continuation's run.json records the
# SEED worktreePath while the sibling marker still names the seed. Continuation
# cleanup removes only that continuation's run dir and defers the shared
# worktree to its owner (success note, not a failure). Missing worktree after
# the seed was cleaned is also a note, not an error. Non-continuation runs that
# point at a foreign worktree still fail closed (state-ownership-violation).

import argparse
import json
import pathlib
import shutil
from typing import List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import envelope as envelope_mod
from groklib.worktree import (
    ExternalWorktree,
    marker_path_for,
    rebuild_worktree_from_record,
    remove_external_worktree,
)

# Design §14.17 - factual only; do not say "unacknowledged."
# Manifest-ready is write-time only (not dual-condition /grok:handoff ready).
_READY_HANDOFF_WARNING = (
    "This run's handoff manifest claims integration.ready (write-time only; not dual-condition). "
    "Cleanup will permanently remove its retained worktree and stored handoff artifacts. "
    "The plugin cannot determine whether the implementation was integrated."
)


def _integration_ready_handoff(run_dir: pathlib.Path) -> bool:
    """True when implementation-handoff.json claims integration.ready (manifest-only).

    This is NOT dual-condition ready (no envelope/patch rehash). Used only as a
    cleanup retention warning.
    """
    path = run_dir / "implementation-handoff.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(data, dict):
        return False
    integration = data.get("integration")
    return isinstance(integration, dict) and integration.get("ready") is True


def _log(function: str, message: str) -> None:
    log_stderr("modes.cleanup", function, message)


def _worktree_fields(worktree: Optional[ExternalWorktree]) -> dict:
    if worktree is None:
        return {}
    return {
        "worktreePath": str(worktree.path),
        "worktreeBranch": worktree.branch,
        "baseRevision": worktree.base_revision,
        "repository": str(worktree.repo_root),
    }


def _fail(run_id: str, exc: GrokWrapperError, worktree: Optional[ExternalWorktree] = None) -> dict:
    _log("run", "cleanup failed for {!r}: {} ({})".format(run_id, exc.error_class, exc))
    return envelope_mod.failure_envelope(
        run_id=str(run_id),
        mode="cleanup",
        error_class=exc.error_class,
        message=str(exc),
        detail=exc.detail or None,
        **_worktree_fields(worktree),
    )


def _rebuild_worktree(record: dict) -> Optional[ExternalWorktree]:
    """Delegate to worktree.rebuild_worktree_from_record (single source)."""
    return rebuild_worktree_from_record(record)


def _is_continuation_run(record: dict) -> bool:
    """True when run.json records a continuesRunId (code --continue-run child)."""
    continues = record.get("continuesRunId")
    return isinstance(continues, str) and bool(continues.strip())


def _worktree_owner_run_id(worktree: ExternalWorktree) -> Optional[str]:
    """Return the sibling marker's runId when readable; None when marker is absent/unreadable."""
    marker_path = marker_path_for(worktree.path)
    if not marker_path.is_file():
        return None
    try:
        return runstate.verify_owner_marker(marker_path)
    except GrokWrapperError:
        return None


def _continuation_worktree_deferral(
    run_id: str, worktree: Optional[ExternalWorktree], record: dict
) -> Tuple[bool, Optional[str]]:
    """Decide whether a continuation should skip worktree removal.

    Returns ``(defer, note)``. When ``defer`` is True the shared worktree is left
    alone (or already gone) and ``note`` explains why; the run dir is still cleaned.
    When ``defer`` is False the normal ownership-bound removal path applies.

    Non-continuations always return ``(False, None)`` so foreign ownership still
    fails closed via remove_external_worktree.
    """
    if worktree is None or not _is_continuation_run(record):
        return False, None

    owner_run_id = _worktree_owner_run_id(worktree)
    path_exists = worktree.path.exists()
    path_run_id = worktree.path.name

    # Fully gone (seed cleaned, or never present): note missing, clean run dir only.
    if not path_exists and owner_run_id is None:
        note = (
            "worktree missing (already removed or cleaned with its owner run); "
            "continuation run dir cleaned only"
        )
        _log(
            "_continuation_worktree_deferral",
            "continuation {!r}: {}".format(run_id, note),
        )
        return True, note

    # Marker (or path name) names another run -- typically the seed. Defer.
    foreign_owner = owner_run_id is not None and owner_run_id != run_id
    foreign_path = path_run_id != run_id
    if foreign_owner or foreign_path:
        owner_label = owner_run_id or path_run_id
        note = "worktree owned by run {}; clean that run to remove it".format(owner_label)
        _log(
            "_continuation_worktree_deferral",
            "continuation {!r} defers shared worktree to {!r}".format(run_id, owner_label),
        )
        return True, note

    # Marker and path both name this continuation: normal removal.
    return False, None


_TERMINAL_LIFECYCLES = frozenset({"completed", "failed", "canceled"})


def _refuse_active_run(run_id: str, run_dir: pathlib.Path, record: dict) -> None:
    """Raise if --confirm would delete a still-active run (C3).

    - Finalize worker alive/unknown: always refuse (durable write may still land).
    - Non-terminal lifecycle + owner not dead: refuse (run still in progress).
    Terminal runs may be cleaned even if owner.pid still names a live process
    (the CLI process that finished the run often outlives terminalization in tests).
    Stuck non-terminal runs (owner dead AND worker dead) may be cleaned.
    """
    from groklib.modes.finalize_worker import finalize_worker_liveness

    worker_liveness = finalize_worker_liveness(run_dir)
    if worker_liveness != "dead":
        raise GrokWrapperError(
            "state-ownership-violation",
            "refusing to remove run {}: finalize worker is still {!r}".format(run_id, worker_liveness),
            {
                "runId": run_id,
                "workerLiveness": worker_liveness,
                "lifecycle": record.get("lifecycle"),
            },
        )
    life = record.get("lifecycle")
    if life in _TERMINAL_LIFECYCLES:
        return
    owner_liveness = runstate._home_owner_liveness(run_dir)
    if owner_liveness != "dead":
        raise GrokWrapperError(
            "state-ownership-violation",
            "refusing to remove run {}: owner process is still {!r} (lifecycle={!r})".format(
                run_id, owner_liveness, life
            ),
            {"runId": run_id, "ownerLiveness": owner_liveness, "lifecycle": life},
        )


def _remove_run_dir(run_dir: pathlib.Path) -> None:
    failed_paths: List[str] = []

    def _on_error(_func: object, path: str, _exc_info: object) -> None:
        failed_paths.append(path)

    shutil.rmtree(str(run_dir), onerror=_on_error)
    if failed_paths:
        _log("_remove_run_dir", "failed to remove {} path(s) under {}".format(len(failed_paths), run_dir))
        raise GrokWrapperError(
            "cleanup-failure",
            "failed to remove run directory {}".format(run_dir),
            {"runDir": str(run_dir), "failedEntries": len(failed_paths)},
        )


def _dry_run(
    run_id: str,
    run_dir: pathlib.Path,
    worktree: Optional[ExternalWorktree],
    record: dict,
) -> dict:
    defer, worktree_note = _continuation_worktree_deferral(run_id, worktree, record)
    worktree_report = None
    if worktree is not None and not defer:
        worktree_report = remove_external_worktree(worktree, confirmed=False, expected_run_id=run_id)
    response = {
        "dryRun": True,
        "runDir": str(run_dir),
        "worktree": worktree_report,
        "worktreeRemoved": False,
    }
    if worktree_note:
        response["worktreeNote"] = worktree_note
        response["worktreeDeferred"] = True
    warnings: List[str] = []
    if worktree_note:
        warnings.append(worktree_note)
    if _integration_ready_handoff(run_dir):
        warnings.append(_READY_HANDOFF_WARNING)
        response["integrationReadyHandoff"] = True
    detail = "dry-run: nothing removed; pass --confirm to remove"
    if worktree_note:
        detail = "dry-run: nothing removed; {}".format(worktree_note)
    return envelope_mod.build_envelope(
        run_id=run_id,
        mode="cleanup",
        status="success",
        response=response,
        warnings=warnings,
        cleanup={"status": "retained", "detail": detail},
        **_worktree_fields(worktree),
    )


def _confirmed(
    run_id: str,
    run_dir: pathlib.Path,
    worktree: Optional[ExternalWorktree],
    record: dict,
) -> dict:
    ready_handoff_warning = _READY_HANDOFF_WARNING if _integration_ready_handoff(run_dir) else None
    defer, worktree_note = _continuation_worktree_deferral(run_id, worktree, record)
    worktree_report = None
    worktree_removed = False
    if worktree is not None and not defer:
        try:
            worktree_report = remove_external_worktree(worktree, confirmed=True, expected_run_id=run_id)
            worktree_removed = True
        except GrokWrapperError as exc:
            if exc.error_class != "worktree-failure":
                raise
            # Dirty or otherwise unremovable worktree: retain everything
            # (worktree AND run dir) and report the refusal. Fail closed rather
            # than remove the run dir while its worktree is stranded.
            _log("_confirmed", "worktree retained for {}: {}".format(run_id, exc))
            return envelope_mod.failure_envelope(
                run_id=run_id,
                mode="cleanup",
                error_class="worktree-failure",
                message=str(exc),
                detail=exc.detail or None,
                response={"runDir": str(run_dir), "worktreeRemoved": False, "runDirRemoved": False},
                cleanup={"status": "retained", "detail": "worktree retained (dirty or unremovable); run dir retained"},
                **_worktree_fields(worktree),
            )

    try:
        _remove_run_dir(run_dir)
    except GrokWrapperError as exc:
        # F5: the worktree (when present) was already removed above; only the
        # run-dir removal failed. Report the partial state honestly instead of a
        # "not-applicable" cleanup that hides the removed worktree + retained run
        # dir. This returns (does not raise), so run()'s handler is bypassed.
        _log("_confirmed", "run dir removal failed after worktree removal for {}: {}".format(run_id, exc))
        detail = (
            "worktree removed; run dir removal failed and is retained"
            if worktree_removed
            else "run dir removal failed and is retained"
        )
        return envelope_mod.failure_envelope(
            run_id=run_id,
            mode="cleanup",
            error_class=exc.error_class,
            message=str(exc),
            detail=exc.detail or None,
            response={
                "runDir": str(run_dir),
                "worktree": worktree_report,
                "worktreeRemoved": worktree_removed,
                "runDirRemoved": False,
                **({"worktreeNote": worktree_note} if worktree_note else {}),
            },
            cleanup={"status": "failed", "detail": detail},
            **_worktree_fields(worktree),
        )

    # S5: when the worktree was removed but its branch could NOT be deleted
    # (unmerged Grok commits), the branch is orphaned -- the top-level cleanup
    # status must say "retained", not falsely claim "clean".
    branch_retained = bool(worktree_report and worktree_report.get("branchRetained"))
    response = {
        "runDir": str(run_dir),
        "worktree": worktree_report,
        "worktreeRemoved": worktree_removed,
        "runDirRemoved": True,
    }
    if worktree_note:
        response["worktreeNote"] = worktree_note
        response["worktreeDeferred"] = True
    if ready_handoff_warning:
        response["integrationReadyHandoff"] = True
    warnings: List[str] = []
    if worktree_note:
        warnings.append(worktree_note)
    if ready_handoff_warning:
        warnings.append(ready_handoff_warning)
    if branch_retained:
        cleanup_field = {
            "status": "retained",
            "detail": "run dir and worktree removed; branch {} retained: {}".format(
                worktree_report.get("worktreeBranch"), worktree_report.get("branchRetainReason")
            ),
        }
    elif worktree_note:
        cleanup_field = {
            "status": "clean",
            "detail": "run dir removed; {}".format(worktree_note),
        }
    else:
        detail = "run dir removed" + (" and worktree removed" if worktree_removed else "")
        cleanup_field = {"status": "clean", "detail": detail}
    return envelope_mod.build_envelope(
        run_id=run_id,
        mode="cleanup",
        status="success",
        response=response,
        warnings=warnings,
        cleanup=cleanup_field,
        **_worktree_fields(worktree),
    )


def _reap_partial_create_debris(run_id: str, run_dir: pathlib.Path, confirmed: bool) -> dict:
    """Report (dry-run) or remove (--confirm) a create_run partial-failure run directory.

    Round5 unreapable-run-dir-on-create-run-partial-failure / Grok dogfood-3 #3: a
    run dir whose create_run failed before a valid owner.json/run.json existed is
    unreachable by the normal cleanup path (load_run_record fails first). This
    reaps that debris, delegating the actual removal (with a re-checked ownership
    guard) to runstate.remove_partial_run_dir so a foreign dir is never removed.
    """
    if not confirmed:
        return envelope_mod.build_envelope(
            run_id=run_id,
            mode="cleanup",
            status="success",
            response={"partialCreate": True, "runDir": str(run_dir), "runDirRemoved": False},
            cleanup={
                "status": "retained",
                "detail": "partial-create run dir (no run record); pass --confirm to remove",
            },
        )
    try:
        runstate.remove_partial_run_dir(run_id)
    except GrokWrapperError as exc:
        return _fail(run_id, exc)
    return envelope_mod.build_envelope(
        run_id=run_id,
        mode="cleanup",
        status="success",
        response={"partialCreate": True, "runDir": str(run_dir), "runDirRemoved": True},
        cleanup={"status": "clean", "detail": "partial-create run dir removed"},
    )


def run(args: argparse.Namespace) -> dict:
    """Verify ownership, then dry-run report or (with --confirm) remove the run's artifacts."""
    run_id = args.run_id
    confirmed = bool(getattr(args, "confirm", False))
    run_dir = runstate.state_root() / "runs" / run_id

    try:
        record = runstate.load_run_record(run_id)
    except GrokWrapperError as exc:
        # A create_run that failed before a valid run.json/owner.json existed
        # leaves an orphan dir that load_run_record can never reach; reap it here
        # so the wrapper's own tooling can clean up its own partial state.
        if runstate.is_orphaned_partial_run_dir(run_id):
            return _reap_partial_create_debris(run_id, run_dir, confirmed)
        return _fail(run_id, exc)
    try:
        owner_run_id = runstate.verify_owner_marker(run_dir / "owner.json")
    except GrokWrapperError as exc:
        return _fail(run_id, exc)
    # S6: cross-check the marker names the requested run id (as write_run_record
    # does), so a marker for a different run can never authorize removing this
    # run's directory.
    if owner_run_id != run_id:
        _log("run", "run dir marker run id {!r} does not match requested {!r}".format(owner_run_id, run_id))
        return _fail(
            run_id,
            GrokWrapperError(
                "state-ownership-violation",
                "run dir ownership marker run id does not match the requested run id",
                {"markerRunId": owner_run_id, "requestedRunId": run_id},
            ),
        )

    worktree = _rebuild_worktree(record)
    try:
        if not confirmed:
            return _dry_run(run_id, run_dir, worktree, record)
        # Refuse to delete an actively owned or still-finalizing run (C3).
        _refuse_active_run(run_id, run_dir, record)
        return _confirmed(run_id, run_dir, worktree, record)
    except GrokWrapperError as exc:
        return _fail(run_id, exc, worktree)
