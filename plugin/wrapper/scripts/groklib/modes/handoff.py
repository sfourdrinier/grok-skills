# wrapper/scripts/groklib/modes/handoff.py
#
# Read-only `/grok:handoff --run-id` (design §14.14): load implementation-handoff
# artifacts, rehash the patch, apply dual-condition ready (manifest ready +
# completed terminal envelope). Never spawns Grok, never creates jobs, never
# writes the run directory, never applies patches.

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple  # Tuple used by load helpers + patch resolve

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import envelope as envelope_mod
from groklib.implementation_handoff import dual_condition_ready, validate_implementation_handoff


def _log(function: str, message: str) -> None:
    log_stderr("modes.handoff", function, message)


def _load_json(path: pathlib.Path) -> Tuple[Optional[dict], Optional[str]]:
    if not path.is_file():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "not-object"
    return data, None


def _resolve_patch_under_run(run_dir: pathlib.Path, relative_path: str) -> Tuple[Optional[pathlib.Path], Optional[str]]:
    """Resolve patch.relativePath strictly under run_dir; reject absolute/escape paths."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None, "empty-relative-path"
    raw = relative_path.strip().replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return None, "absolute-path-rejected"
    parts = []
    for p in pathlib.PurePosixPath(raw).parts:
        if p in ("", "."):
            continue
        if p == "..":
            return None, "parent-segment-rejected"
        parts.append(p)
    if not parts:
        return None, "empty-relative-path"
    candidate = (run_dir.joinpath(*parts)).resolve()
    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError:
        return None, "escapes-run-dir"
    return candidate, None


def _load_stored_envelope(run_id: str, path: pathlib.Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if envelope_mod.validate_envelope(raw):
        return None
    if raw.get("runId") != run_id:
        return None
    return raw


def run(args: argparse.Namespace) -> dict:
    """Read-only handoff observation for ``args.run_id``."""
    run_id = args.run_id
    try:
        record = runstate.load_run_record(run_id)
    except GrokWrapperError as exc:
        _log("run", "cannot load run {}: {} ({})".format(run_id, exc.error_class, exc))
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="handoff-unavailable" if exc.error_class == "invalid-target" else exc.error_class,
            message=str(exc),
            detail=exc.detail or None,
            progressStreamPath=None,
        )

    run_dir = runstate.state_root() / "runs" / run_id
    try:
        owner = runstate.verify_owner_marker(run_dir / "owner.json")
    except GrokWrapperError as exc:
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="handoff-unavailable",
            message=str(exc),
            detail=exc.detail or None,
            progressStreamPath=None,
        )
    if owner != run_id:
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="handoff-unavailable",
            message="run ownership marker does not match requested run id",
            detail={"markerRunId": owner, "requestedRunId": run_id},
            progressStreamPath=None,
        )

    if record.get("mode") != "code":
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="handoff-unavailable",
            message="implementation handoff is only available for code runs",
            detail={"mode": record.get("mode")},
            progressStreamPath=None,
        )

    manifest_path = run_dir / "implementation-handoff.json"
    manifest, load_err = _load_json(manifest_path)
    if manifest is None:
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="handoff-unavailable",
            message="implementation handoff artifacts are not available for this run",
            detail={"reason": load_err or "missing", "path": str(manifest_path)},
            progressStreamPath=None,
        )

    errs = validate_implementation_handoff(manifest)
    if errs:
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="artifact-integrity-failure",
            message="implementation handoff manifest failed validation",
            detail={"errors": errs},
            progressStreamPath=None,
        )

    patch_rel = (manifest.get("patch") or {}).get("relativePath") or "artifacts/implementation.patch"
    patch_path, path_err = _resolve_patch_under_run(run_dir, patch_rel)
    if path_err is not None:
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class="artifact-integrity-failure",
            message="handoff patch path is invalid or escapes the run directory",
            detail={"reason": path_err, "relativePath": patch_rel},
            progressStreamPath=None,
        )
    stored_envelope = _load_stored_envelope(run_id, run_dir / "envelope.json")

    ready, dual_blockers = dual_condition_ready(
        manifest=manifest,
        envelope=stored_envelope,
        patch_abs=patch_path if patch_path is not None and patch_path.is_file() else None,
    )

    if not ready:
        # Prefer integrity when rehash fails; terminal-envelope-incomplete when that is the issue
        primary = "handoff-unavailable"
        for b in dual_blockers:
            kind = b.get("kind") if isinstance(b, dict) else None
            if kind == "artifact-integrity-failure":
                primary = "artifact-integrity-failure"
                break
            if kind == "terminal-envelope-incomplete":
                primary = "terminal-envelope-incomplete"
                break
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="handoff",
            error_class=primary,
            message="implementation handoff is not integration-ready",
            detail={
                "integration": {"ready": False, "blockers": dual_blockers},
                "manifestReady": bool((manifest.get("integration") or {}).get("ready")),
                "hasCompletedEnvelope": bool(
                    stored_envelope and stored_envelope.get("status") == "success"
                ),
            },
            progressStreamPath=None,
            repository=record.get("repository"),
            baseRevision=record.get("baseRevision"),
            worktreePath=record.get("worktreePath"),
            worktreeBranch=record.get("worktreeBranch"),
        )

    # Success: surface handoff payload under response (read-only observation)
    response: Dict[str, Any] = {
        "integration": {"ready": True, "blockers": []},
        "handoff": {
            "runId": run_id,
            "taskId": manifest.get("taskId"),
            "baseRevision": manifest.get("baseRevision"),
            "resultTreeOid": manifest.get("resultTreeOid"),
            "patch": manifest.get("patch"),
            "changedFiles": manifest.get("changedFiles"),
            "validation": manifest.get("validation"),
            "worktree": manifest.get("worktree"),
            "contractSha256": manifest.get("contractSha256"),
            "createdAtUtc": manifest.get("createdAtUtc"),
            "manifestPath": str(manifest_path),
            "patchPath": str(patch_path),
        },
        "parentProtocol": {
            "autoApply": False,
            "note": (
                "Parent must re-validate after apply. Use git apply --check --binary then "
                "explicit apply; never auto-commit or push from this plugin."
            ),
        },
    }

    return envelope_mod.build_envelope(
        run_id=str(run_id),
        mode="handoff",
        status="success",
        repository=record.get("repository"),
        baseRevision=manifest.get("baseRevision") or record.get("baseRevision"),
        worktreePath=record.get("worktreePath"),
        worktreeBranch=record.get("worktreeBranch"),
        response=response,
        progressStreamPath=None,
        cleanup={"status": "not-applicable", "detail": None},
    )
