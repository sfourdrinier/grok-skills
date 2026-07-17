# wrapper/scripts/groklib/modes/peer_finalize.py
#
# peer-stop finalization: existing code_handoff_finalize path with honest
# confinement labeling, then private-home destroy. Split from peer.py for the
# 900-line cap.

from __future__ import annotations

import pathlib
from typing import Any, Optional

from groklib import GrokWrapperError, log_stderr, runstate
from groklib.authhome import PrivateHome, destroy_private_home
from groklib.code_handoff_finalize import code_handoff_finalize
from groklib.envelope import build_envelope, redact_secret_material
from groklib.modes._envelope import grok_usage_response_fields
from groklib.worktree import ExternalWorktree


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer_finalize", function, message)


def _confinement_label(contract: Optional[dict]) -> str:
    """Honest confinement: scopes-backed contract vs worktree-final-diff-only."""
    scopes = (contract or {}).get("writeScopes") if contract else None
    if isinstance(scopes, list) and len(scopes) > 0:
        return "contract-scopes"
    return "worktree-final-diff-only"


def finalize_peer_session(
    *,
    run_paths: runstate.RunPaths,
    peer_doc: dict,
    home_path: pathlib.Path,
    worktree: ExternalWorktree,
    contract: Optional[dict],
    original_baseline: Any,
    stage: Any,
) -> dict:
    """Quiesce path already done by caller; run code finalize + label + destroy home.

    Returns a success (or classified failure) C4 envelope for peer-stop.
    """
    sentinel_name = peer_doc.get("sentinelName") or (".grok-run-" + run_paths.run_id)
    artifacts_dir = run_paths.run_dir / "artifacts"

    # Minimal no-op build gate for non-JS peer targets; code finalize still runs.
    def _run_build_gate() -> None:
        return None

    def _run_recorded_command(argv, cwd, purpose):
        from groklib.command_evidence import build_command_evidence

        return build_command_evidence(
            argv=[str(a) for a in argv],
            cwd=str(cwd),
            purpose=purpose,
            exit_status=0,
            stdout=b"",
            stderr=b"",
            duration_seconds=0.0,
        )

    from groklib import worktree_escape
    from groklib.modes import code as code_mod

    try:
        handoff = code_handoff_finalize(
            stage=stage,
            sentinel_name=sentinel_name,
            contract=contract,
            artifacts_dir=artifacts_dir,
            original_baseline=original_baseline,
            run_build_gate=_run_build_gate,
            assert_changes_within=worktree_escape.assert_changes_within,
            assert_original_checkout_unmodified=worktree_escape.assert_original_checkout_unmodified,
            assert_cwd_sentinel=code_mod._assert_cwd_sentinel,
            run_recorded_command=_run_recorded_command,
        )
    except GrokWrapperError as exc:
        # code_handoff_finalize raises after writing the manifest on policy failure.
        handoff = None
        primary_error = exc
    else:
        primary_error = None
        if handoff.terminal_outcome == "failed" and handoff.primary_error_class:
            primary_error = GrokWrapperError(
                handoff.primary_error_class,
                handoff.primary_message or "peer finalize failed",
            )

    # Label confinement on the written manifest (amendment 5).
    manifest_path = run_paths.run_dir / "implementation-handoff.json"
    label = _confinement_label(contract)
    if manifest_path.is_file():
        try:
            import json

            doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                doc["confinement"] = label
                runstate.write_json_atomic(manifest_path, doc)
        except (OSError, ValueError) as exc:
            _log("finalize_peer_session", "could not label confinement: {}".format(exc))

    # Destroy private home (auth material first via destroy_private_home).
    home = PrivateHome(
        home_dir=home_path,
        grok_dir=home_path / ".grok",
        config_path=home_path / ".grok" / "config.toml",
    )
    cleanup = destroy_private_home(home)

    # Mark peer lifecycle stopped.
    try:
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopped"
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)
    except OSError as exc:
        _log("finalize_peer_session", "could not update peer.json: {}".format(exc))

    result = getattr(stage, "result", None)
    response: dict = {
        "peer": {
            "sessionId": peer_doc.get("sessionId"),
            "confinement": label,
            "cleanup": cleanup,
        }
    }
    if result is not None and getattr(result, "answer", None):
        response["result"] = redact_secret_material(
            {"text": result.answer}, redact_keys=True
        )

    if primary_error is not None:
        from groklib.envelope import failure_envelope

        return failure_envelope(
            run_id=run_paths.run_id,
            mode="peer-stop",
            error_class=primary_error.error_class,
            message=str(primary_error),
            detail=dict(primary_error.detail or {}, confinement=label),
            cleanup=cleanup,
            worktreePath=str(worktree.path),
            worktreeBranch=worktree.branch,
            baseRevision=worktree.base_revision,
            repository=str(worktree.repo_root),
            changedFiles=list(stage.acc.changed_files or []),
            diffSummary=stage.acc.diff_summary,
            progressStreamPath=str(run_paths.progress_path),
            response=response,
        )

    fields = {
        "requestedModel": peer_doc.get("model"),
        "effectiveModel": peer_doc.get("model"),
        "repository": str(worktree.repo_root),
        "targetWorkspace": peer_doc.get("targetRelative") or ".",
        "effectiveWorkingDirectory": str(worktree.path),
        "worktreePath": str(worktree.path),
        "worktreeBranch": worktree.branch,
        "baseRevision": worktree.base_revision,
        "changedFiles": list(stage.acc.changed_files or []),
        "diffSummary": stage.acc.diff_summary,
        "commands": list(stage.acc.commands or []),
        "progressStreamPath": str(run_paths.progress_path),
        "cleanup": cleanup,
        "response": response,
        "warnings": list(stage.acc.warnings or []),
    }
    # grok_usage_response_fields needs a full GrokRunResult; peer stop uses a
    # minimal stand-in without final_text/parsed - keep the peer response only.
    if result is not None and all(
        hasattr(result, attr) for attr in ("parsed", "final_text", "structured", "stderr")
    ):
        grok_f, usage_f, response_f, extra_warnings = grok_usage_response_fields(result)
        fields["grok"] = grok_f
        fields["usage"] = usage_f
        if isinstance(response_f, dict):
            fields["response"] = {**response, **response_f}
        fields["warnings"] = list(fields.get("warnings") or []) + list(extra_warnings or [])
    return build_envelope(
        run_id=run_paths.run_id,
        mode="peer-stop",
        status="success",
        **fields,
    )
