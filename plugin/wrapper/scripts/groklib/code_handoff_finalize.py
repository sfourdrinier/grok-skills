# wrapper/scripts/groklib/code_handoff_finalize.py
#
# Ordered post-Grok finalization for code mode (design §14.6). Called only from
# modes/code._finalize - no second pipeline.

from __future__ import annotations

import json
import pathlib
import stat
from typing import Any, Callable, Dict, List, Optional

from groklib import GrokWrapperError, log_stderr, platformsupport
from groklib.command_evidence import build_command_evidence
from groklib.handoff_patch import capture_phase1_patch, list_changed_paths
from groklib.implementation_contract import normalize_repo_relative, path_in_scopes, trust_model
from groklib.implementation_handoff import (
    HARD_BLOCKER_KINDS,
    HandoffBlocker,
    HandoffBuildResult,
    _HARD_PRIMARY_MAPPING,
    _STEP_ORDER,
    compute_integration_ready,
    primary_error_from_blockers,
    write_manifest,
)

_log = lambda fn, msg: log_stderr("code_handoff_finalize", fn, msg)
_PATCH_FORMAT = "git-binary-full-index-v1"


def _now_utc() -> str:
    import datetime
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _run_git_env(repo, args, env=None):
    from groklib.handoff_patch import _run_git_env as _rg
    return _rg(repo, args, env=env)


def _git_ok(repo, args, env=None):
    from groklib.handoff_patch import _git_ok as _g
    return _g(repo, args, env=env)


def _head_sha(worktree_path: pathlib.Path) -> str:
    return _git_ok(worktree_path, ["rev-parse", "HEAD"]).strip()


def _remove_exact_sentinel(worktree_path: pathlib.Path, sentinel_name: str) -> None:
    path = worktree_path / sentinel_name
    try:
        st = path.lstat()
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise GrokWrapperError(
            "wrong-working-directory",
            "cwd sentinel is not a regular file and cannot be removed safely",
            {"sentinel": sentinel_name, "path": str(path)},
        )
    try:
        path.unlink()
    except OSError as exc:
        raise GrokWrapperError(
            "wrong-working-directory",
            "could not remove cwd sentinel: {}".format(exc),
            {"sentinel": sentinel_name},
        ) from exc


def _contract_sha256(contract: Optional[dict]) -> Optional[str]:
    if not contract:
        return None
    import hashlib
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def code_handoff_finalize(
    *,
    stage: Any,
    sentinel_name: str,
    contract: Optional[dict],
    artifacts_dir: pathlib.Path,
    original_baseline: Any,
    run_build_gate: Callable[..., None],
    assert_changes_within: Callable[..., None],
    assert_original_checkout_unmodified: Callable[..., None],
    assert_cwd_sentinel: Callable[..., None],
    run_recorded_command: Callable[..., dict],
    step_log: Optional[List[str]] = None,
) -> HandoffBuildResult:
    """Execute design §14.6 order on the FinalizeStage path. Writes handoff before return/raise.

    Policy failures accumulate as blockers and continue forensics when safe.
    After writing the phase-2 manifest, raises primary GrokWrapperError when
    terminalOutcome is failed (so the worktree runner emits a failure envelope).
    """
    from groklib import worktree as worktree_mod

    steps: List[str] = list(step_log) if step_log is not None else []
    blockers: List[HandoffBlocker] = []
    worktree = stage.worktree
    base_revision = worktree.base_revision
    run_id = stage.run_id
    scopes = list((contract or {}).get("writeScopes") or [])
    task_id = (contract or {}).get("taskId") or "no-contract"

    head_ok = True
    scopes_ok = True
    sentinel_ok = True
    patch_ok = False
    validation_ok = True
    build_gate_ok = True
    shared_safety_ok = True
    original_checkout_ok = True
    patch_meta: Optional[dict] = None
    patch_path: Optional[pathlib.Path] = None
    result_tree: Optional[str] = None
    changed: List[dict] = []
    validation_evidence: List[dict] = []

    # 1. verify sentinel (hard fail - no spoofed workspace)
    steps.append("verify-sentinel")
    try:
        assert_cwd_sentinel(worktree, sentinel_name)
    except GrokWrapperError:
        sentinel_ok = False
        raise

    # 2. remove exact sentinel only
    steps.append("remove-sentinel")
    _remove_exact_sentinel(worktree.path, sentinel_name)

    # 3. HEAD still equals baseRevision
    steps.append("head-check")
    try:
        head = _head_sha(worktree.path)
        if head != base_revision:
            head_ok = False
            blockers.append(
                HandoffBlocker(
                    "unexpected-commit",
                    "worktree HEAD moved from baseRevision during the run",
                    {"head": head, "baseRevision": base_revision},
                )
            )
            _log("code_handoff_finalize", "unexpected-commit head={} base={}".format(head, base_revision))
    except GrokWrapperError as exc:
        head_ok = False
        blockers.append(
            HandoffBlocker("unexpected-commit", "could not read HEAD: {}".format(exc), {})
        )

    # 4. changed files (sentinel must not appear)
    steps.append("changed-files")
    try:
        changed = list_changed_paths(worktree.path, base_revision)
        # Filter any residual sentinel name
        changed = [c for c in changed if c.get("path") != sentinel_name]
        # Envelope uses path strings
        stage.acc.changed_files = [c["path"] for c in changed]
        try:
            _summary_files, diff_text = worktree_mod.diff_summary(worktree)
            stage.acc.diff_summary = diff_text
        except Exception as exc:
            _log("code_handoff_finalize", "diff_summary failed: {}".format(exc))
        stage.acc.effective_working_directory = str(worktree.path)
    except Exception as exc:
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "could not list changed files: {}".format(exc), {})
        )
        changed = []

    # 5. write scopes (contract) - destination and rename/copy source (oldPath)
    steps.append("write-scopes")
    if contract and scopes:
        for entry in changed:
            paths_to_check = []
            p = entry.get("path") or ""
            if p:
                paths_to_check.append(("path", p))
            old_p = entry.get("oldPath")
            if isinstance(old_p, str) and old_p:
                paths_to_check.append(("oldPath", old_p))
            for which, candidate in paths_to_check:
                if not path_in_scopes(candidate, scopes):
                    scopes_ok = False
                    blockers.append(
                        HandoffBlocker(
                            "write-scope-violation",
                            "changed path outside writeScopes",
                            {"path": candidate, "field": which, "status": entry.get("status")},
                        )
                    )

    # Confinement scan of worktree changes (pre-build-gate; original checkout
    # re-scan is after the gate so post-build-gate phase is preserved).
    try:
        assert_changes_within(worktree, (worktree.path,), original_baseline=original_baseline)
    except GrokWrapperError as exc:
        shared_safety_ok = False
        kind = exc.error_class if exc.error_class in (
            "unexpected-edits",
            "sandbox-failure",
            "worktree-failure",
        ) else "validation-failure"
        blockers.append(
            HandoffBlocker(
                kind,
                "worktree escape / confinement failed: {}".format(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )

    # 6. phase-1 forensic patch
    steps.append("forensic-patch")
    try:
        patch_meta, patch_path, result_tree, patch_blockers, patch_steps = capture_phase1_patch(
            worktree_path=worktree.path,
            base_revision=base_revision,
            artifacts_dir=artifacts_dir,
            run_id=run_id,
        )
        steps.extend(patch_steps)
        blockers.extend(patch_blockers)
        fatal_patch = {
            "secret-material",
            "artifact-too-large",
            "artifact-generation-failure",
        }
        patch_ok = patch_meta is not None and not any(b.kind in fatal_patch for b in patch_blockers)
    except Exception as exc:
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "patch capture raised: {}".format(exc), {})
        )
        patch_ok = False

    # 7. requiredValidation (operator-trusted)
    steps.append("required-validation")
    if contract and contract.get("requiredValidation"):
        for entry in contract["requiredValidation"]:
            argv = list(entry["argv"])
            rel_cwd = entry.get("cwd") or "."
            if rel_cwd in (".", "./", ""):
                cwd = worktree.path
            else:
                try:
                    rel = normalize_repo_relative(rel_cwd)
                except GrokWrapperError as exc:
                    validation_ok = False
                    blockers.append(
                        HandoffBlocker("validation-failure", "invalid validation cwd", {"error": str(exc)})
                    )
                    continue
                cwd = (worktree.path / rel).resolve()
                try:
                    cwd.relative_to(worktree.path.resolve())
                except ValueError:
                    validation_ok = False
                    blockers.append(
                        HandoffBlocker(
                            "validation-failure",
                            "validation cwd escapes worktree",
                            {"cwd": str(cwd)},
                        )
                    )
                    continue
            purpose = entry.get("purpose") or "contract-validation"
            try:
                rec = run_recorded_command(argv, cwd, purpose)
            except GrokWrapperError as exc:
                # Launch/spawn failure: record blocker and continue so phase-2
                # handoff manifest is still written for /grok:handoff forensics.
                validation_ok = False
                blockers.append(
                    HandoffBlocker(
                        "validation-failure",
                        "requiredValidation command could not be run: {}".format(exc),
                        dict(exc.detail or {}, errorClass=exc.error_class, argv=argv),
                    )
                )
                stage.acc.commands.append(
                    build_command_evidence(
                        argv=argv,
                        cwd=str(cwd),
                        purpose=purpose,
                        exit_status=-1,
                        detail=str(exc),
                    )
                )
                continue
            if "stdoutSha256" not in rec:
                rec = {
                    **rec,
                    **build_command_evidence(
                        argv=argv,
                        cwd=str(cwd),
                        purpose=purpose,
                        exit_status=int(rec.get("exitStatus", 1)),
                    ),
                }
            stage.acc.commands.append(rec)
            validation_evidence.append(rec)
            try:
                assert_original_checkout_unmodified(
                    worktree, (worktree.path,), original_baseline=original_baseline
                )
            except GrokWrapperError as exc:
                validation_ok = False
                original_checkout_ok = False
                blockers.append(
                    HandoffBlocker(
                        "validation-failure",
                        "original checkout modified after requiredValidation",
                        {"error": str(exc)},
                    )
                )
            if int(rec.get("exitStatus", 1)) != 0:
                validation_ok = False
                blockers.append(
                    HandoffBlocker(
                        "validation-failure",
                        "requiredValidation command failed",
                        {"argv": argv, "exitStatus": rec.get("exitStatus")},
                    )
                )
    else:
        validation_ok = True  # no contract validations

    # 8. wrapper build gate
    steps.append("build-gate")
    try:
        run_build_gate()
        build_gate_ok = True
    except GrokWrapperError as exc:
        build_gate_ok = False
        blockers.append(
            HandoffBlocker(
                "validation-failure",
                "build gate failed: {}".format(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )

    # 9. shared safety - post-build-gate original-checkout re-scan (phase tag)
    steps.append("shared-safety")
    try:
        assert_original_checkout_unmodified(
            worktree, (worktree.path,), original_baseline=original_baseline
        )
    except GrokWrapperError as exc:
        original_checkout_ok = False
        shared_safety_ok = False
        kind = exc.error_class if exc.error_class in (
            "unexpected-edits",
            "sandbox-failure",
            "worktree-failure",
        ) else "validation-failure"
        blockers.append(
            HandoffBlocker(
                kind,
                "original checkout modified after run: {}".format(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )

    # no-changes
    if not changed:
        blockers.append(HandoffBlocker("no-changes", "no changed files to hand off", {}))

    # 10. terminalOutcome
    steps.append("terminal-outcome")
    # Ready-only soft blockers (SOFT_BLOCKER_KINDS) never fail the code envelope.
    # Hard policy failures raise after handoff write so the runner emits failure.
    policy_fail = [b for b in blockers if b.kind in HARD_BLOCKER_KINDS]
    terminal_outcome = "failed" if policy_fail else "completed"

    # 11. compute ready
    steps.append("compute-ready")
    ready = compute_integration_ready(
        terminal_outcome=terminal_outcome,
        head_matches_base=head_ok,
        scopes_ok=scopes_ok if contract else True,
        original_checkout_ok=original_checkout_ok,
        sentinel_ok=sentinel_ok,
        patch_ok=patch_ok and patch_meta is not None,
        validation_ok=validation_ok,
        build_gate_ok=build_gate_ok,
        shared_safety_ok=shared_safety_ok,
        blockers=blockers,
        changed_count=len(changed),
    )

    # Ensure result tree
    if not result_tree:
        try:
            result_tree = _git_ok(worktree.path, ["rev-parse", "HEAD^{tree}"]).strip()
        except GrokWrapperError:
            result_tree = base_revision  # placeholder; validation may fail

    if patch_meta is None:
        # Minimal stub so manifest validates structure when patch failed
        patch_meta = {
            "format": _PATCH_FORMAT,
            "relativePath": "artifacts/implementation.patch",
            "sha256": "0" * 64,
            "bytes": 0,
        }
        # If no real patch, force not ready
        ready = False
        if not any(b.kind.startswith("artifact") or b.kind == "secret-material" for b in blockers):
            blockers.append(
                HandoffBlocker("artifact-generation-failure", "no implementation patch produced", {})
            )
            terminal_outcome = "failed"
            ready = False

    validation_block = {
        "requiredCommandsPassed": validation_ok,
        "buildGatePassed": build_gate_ok,
        "allPassed": validation_ok and build_gate_ok,
        "sources": {
            "wrapperBuildGate": {"authoritative": True, "passed": build_gate_ok},
            "contractRequiredValidation": {
                "authoritative": True,
                "passed": validation_ok,
                "trustModel": trust_model(),
            },
            "modelClaimedCommands": {
                "authoritative": False,
                "note": "ignored for readiness",
            },
        },
    }

    doc = {
        "schemaVersion": 1,
        "runId": run_id,
        "taskId": task_id,
        "contractSha256": _contract_sha256(contract),
        "baseRevision": base_revision,
        "resultTreeOid": result_tree or "",
        "changedFiles": changed,
        "patch": patch_meta,
        "validation": validation_block,
        "integration": {
            "ready": bool(ready),
            "blockers": [b.as_dict() for b in blockers],
        },
        "worktree": {
            "retained": True,
            "path": str(worktree.path),
            "branch": worktree.branch,
        },
        "createdAtUtc": _now_utc(),
    }

    # 12. write handoff JSON
    steps.append("write-manifest")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    try:
        platformsupport.restrict_dir_permissions(artifacts_dir)
    except OSError:
        pass
    manifest_path = artifacts_dir.parent / "implementation-handoff.json"
    # Design places implementation-handoff.json at run root next to artifacts/
    # Prefer run_dir/implementation-handoff.json
    run_dir = artifacts_dir.parent
    manifest_path = run_dir / "implementation-handoff.json"
    try:
        write_manifest(manifest_path, doc)
    except GrokWrapperError as exc:
        # If validation fails due to empty resultTree etc., try to still record
        _log("code_handoff_finalize", "manifest write failed: {}".format(exc))
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "manifest write failed: {}".format(exc), {})
        )
        terminal_outcome = "failed"
        ready = False
        doc["integration"]["ready"] = False
        doc["integration"]["blockers"] = [b.as_dict() for b in blockers]
        # Force minimal valid fields
        if not doc.get("resultTreeOid"):
            doc["resultTreeOid"] = "0" * 40
        try:
            write_manifest(manifest_path, doc)
        except Exception as inner:
            _log("code_handoff_finalize", "second manifest write failed: {}".format(inner))

    primary_class, primary_msg = primary_error_from_blockers(blockers)
    result = HandoffBuildResult(
        blockers=blockers,
        terminal_outcome=terminal_outcome,
        manifest=doc,
        patch_path=patch_path,
        primary_error_class=primary_class,
        primary_message=primary_msg,
        step_log=steps,
    )

    # After handoff write: raise primary so runner emits failure envelope
    if terminal_outcome == "failed" and primary_class:
        # Prefer the first hard blocker's own detail (e.g. phase=post-build-gate,
        # violations[]) so envelope.error.detail matches pre-PR4 callers.
        primary_detail: Dict[str, Any] = {}
        for b in blockers:
            if b.kind not in HARD_BLOCKER_KINDS:
                continue
            if _HARD_PRIMARY_MAPPING.get(b.kind) == primary_class:
                if b.detail:
                    primary_detail.update(b.detail)
                break
        primary_detail["blockers"] = [b.as_dict() for b in blockers]
        primary_detail["stepLog"] = steps
        raise GrokWrapperError(
            primary_class, primary_msg or "implementation handoff not ready", primary_detail
        )

    return result
