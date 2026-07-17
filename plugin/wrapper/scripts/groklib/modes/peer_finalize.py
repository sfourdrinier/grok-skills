# wrapper/scripts/groklib/modes/peer_finalize.py
#
# peer-stop finalization for the experimental ACP peer-preview channel.
# Runs forensic code-path steps (sentinel, scopes, patch, escape) but NEVER
# executes contract requiredValidation or the wrapper build gate, never forges
# exit_status, and always writes a not-ready manifest (authoritative=false
# validation sources + handoff-unavailable). Split from peer.py for the
# 900-line cap.

from __future__ import annotations

import json
import pathlib
import tempfile
from typing import Any, List, Optional

from groklib import GrokWrapperError, log_stderr, runstate
from groklib.authhome import PrivateHome, destroy_private_home
from groklib.code_handoff_finalize import code_handoff_finalize
from groklib.envelope import build_envelope, redact_secret_material
from groklib.implementation_contract import trust_model
from groklib.implementation_handoff import write_manifest
from groklib.modes._envelope import grok_usage_response_fields
from groklib.sandbox import policy_for_mode, verify_enforcement
from groklib.worktree import ExternalWorktree

_PEER_PREVIEW_REASON = "peer-preview: not executed"
_HANDOFF_UNAVAILABLE_MESSAGE = (
    "peer-preview runs are not integration-ready; apply from the worktree "
    "manually after your own review"
)


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer_finalize", function, message)


def _confinement_label(contract: Optional[dict]) -> str:
    """Honest confinement: scopes-backed contract vs worktree-final-diff-only."""
    scopes = (contract or {}).get("writeScopes") if contract else None
    if isinstance(scopes, list) and len(scopes) > 0:
        return "contract-scopes"
    return "worktree-final-diff-only"


def _forensics_contract(contract: Optional[dict]) -> Optional[dict]:
    """Contract for forensic finalize only: scopes kept, requiredValidation stripped.

    Peer-preview must not execute operator requiredValidation commands.
    """
    if not isinstance(contract, dict):
        return None
    out = dict(contract)
    out.pop("requiredValidation", None)
    return out


def _preview_validation_block() -> dict:
    """Honest validation: gates were not executed; sources are non-authoritative."""
    return {
        "requiredCommandsPassed": False,
        "buildGatePassed": False,
        "allPassed": False,
        "sources": {
            "wrapperBuildGate": {
                "authoritative": False,
                "passed": False,
                "reason": _PEER_PREVIEW_REASON,
            },
            "contractRequiredValidation": {
                "authoritative": False,
                "passed": False,
                "reason": _PEER_PREVIEW_REASON,
                "trustModel": trust_model(),
            },
            "modelClaimedCommands": {
                "authoritative": False,
                "note": "ignored for readiness",
            },
        },
    }


def _handoff_unavailable_blocker() -> dict:
    return {
        "kind": "handoff-unavailable",
        "message": _HANDOFF_UNAVAILABLE_MESSAGE,
    }


def _rewrite_preview_manifest(
    *,
    manifest_path: pathlib.Path,
    label: str,
    extra_blockers: Optional[List[dict]] = None,
) -> dict:
    """Force not-ready preview fields and confinement before a scanned write."""
    if not manifest_path.is_file():
        raise GrokWrapperError(
            "artifact-generation-failure",
            "peer finalize produced no handoff manifest to rewrite",
            {"path": str(manifest_path)},
        )
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GrokWrapperError(
            "artifact-generation-failure",
            "peer finalize could not read handoff manifest: {}".format(exc),
            {"path": str(manifest_path)},
        ) from exc
    if not isinstance(doc, dict):
        raise GrokWrapperError(
            "artifact-generation-failure",
            "peer finalize handoff manifest is not an object",
            {"path": str(manifest_path)},
        )

    # Set confinement BEFORE write_manifest scan (never post-mutate after).
    doc["confinement"] = label
    doc["validation"] = _preview_validation_block()

    integration = doc.get("integration")
    if not isinstance(integration, dict):
        integration = {}
    blockers: List[dict] = []
    raw_blockers = integration.get("blockers")
    if isinstance(raw_blockers, list):
        for item in raw_blockers:
            if isinstance(item, dict) and item.get("kind") != "handoff-unavailable":
                blockers.append(dict(item))
    blockers.insert(0, _handoff_unavailable_blocker())
    for extra in extra_blockers or []:
        if isinstance(extra, dict):
            blockers.append(dict(extra))
    doc["integration"] = {"ready": False, "blockers": blockers}
    write_manifest(manifest_path, doc)
    return doc


def _maybe_verify_sandbox(
    *,
    home_path: pathlib.Path,
    worktree: ExternalWorktree,
) -> Optional[dict]:
    """Best-effort verify_enforcement at peer-stop; return a blocker on failure.

    ACP timing may leave no sandbox-events.jsonl; failure is recorded honestly
    as a soft not-ready blocker (integration.ready is already always false).
    """
    if not home_path.is_dir():
        return {
            "kind": "sandbox-failure",
            "message": "peer-preview: private home missing at stop; sandbox not verified",
        }
    home = PrivateHome(
        home_dir=home_path,
        grok_dir=home_path / ".grok",
        config_path=home_path / ".grok" / "config.toml",
    )
    private_tmp = home_path / "tmp"
    if not private_tmp.is_dir():
        try:
            private_tmp.mkdir(parents=True, exist_ok=True)
        except OSError:
            private_tmp = pathlib.Path(tempfile.mkdtemp(prefix="gs-peer-vtmp-"))
    try:
        policy = policy_for_mode("peer", worktree=worktree.path, private_tmp=private_tmp)
        verify_enforcement(home, policy)
    except GrokWrapperError as exc:
        return {
            "kind": "sandbox-failure",
            "message": "peer-preview sandbox verification failed: {}".format(exc),
            "detail": dict(exc.detail or {}, errorClass=exc.error_class),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "kind": "sandbox-failure",
            "message": "peer-preview sandbox verification failed: {}".format(exc),
        }
    return None


def _terminalize_peer_run(run_paths: runstate.RunPaths, envelope: dict) -> None:
    """Persist terminal envelope + lifecycle so cleanup can rebuild the worktree.

    Durable envelope mode matches create_run ("peer-start"); stdout may still
    use peer-stop. Failures are logged only - the operator-facing envelope is
    already built.
    """
    try:
        durable = dict(envelope)
        durable["mode"] = "peer-start"
        # Internal marker must never land on disk.
        durable.pop("_peerStartAlreadyEmittedRunning", None)
        durable.pop("_suppressStdout", None)

        record = runstate.load_run_record(run_paths.run_id)
        rev = int(record.get("recordRevision", 0))
        life = record.get("lifecycle")
        if life == "created":
            record = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(record["recordRevision"])
            life = "running"
        lifecycle = "completed" if envelope.get("status") == "success" else "failed"
        if life == "running" and lifecycle == "completed":
            record = runstate.set_lifecycle(run_paths, rev, "finalizing")
            rev = int(record["recordRevision"])
        if life in ("completed", "failed", "canceled"):
            return
        runstate.persist_terminal_envelope(
            run_paths, rev, durable, lifecycle=lifecycle
        )
    except Exception as exc:
        _log("_terminalize_peer_run", "could not terminalize peer run: {}".format(exc))


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
    """Quiesce path already done by caller; forensic finalize + honest preview label.

    Returns a success (or classified failure) C4 envelope for peer-stop.
    Never forges command exit_status; never claims integration-ready.
    """
    sentinel_name = peer_doc.get("sentinelName") or (".grok-run-" + run_paths.run_id)
    artifacts_dir = run_paths.run_dir / "artifacts"
    label = _confinement_label(contract)
    forensics_contract = _forensics_contract(contract)

    def _run_build_gate() -> None:
        # Peer-preview: intentionally not executed (no forged pass).
        return None

    def _run_recorded_command(argv, cwd, purpose):
        # requiredValidation is stripped; any call is a programming error.
        raise GrokWrapperError(
            "cli-failure",
            "peer-preview does not execute recorded validation commands",
            {"purpose": purpose, "argv": [str(a) for a in argv], "cwd": str(cwd)},
        )

    from groklib import worktree_escape
    from groklib.modes import code as code_mod

    try:
        handoff = code_handoff_finalize(
            stage=stage,
            sentinel_name=sentinel_name,
            contract=forensics_contract,
            artifacts_dir=artifacts_dir,
            original_baseline=original_baseline,
            run_build_gate=_run_build_gate,
            assert_changes_within=worktree_escape.assert_changes_within,
            assert_original_checkout_unmodified=worktree_escape.assert_original_checkout_unmodified,
            assert_cwd_sentinel=code_mod._assert_cwd_sentinel,
            run_recorded_command=_run_recorded_command,
        )
    except GrokWrapperError as exc:
        handoff = None
        primary_error = exc
    else:
        primary_error = None
        if handoff.terminal_outcome == "failed" and handoff.primary_error_class:
            primary_error = GrokWrapperError(
                handoff.primary_error_class,
                handoff.primary_message or "peer finalize failed",
            )

    sandbox_blocker = _maybe_verify_sandbox(home_path=home_path, worktree=worktree)
    extra_blockers: List[dict] = []
    if sandbox_blocker is not None:
        extra_blockers.append(sandbox_blocker)

    manifest_path = run_paths.run_dir / "implementation-handoff.json"
    try:
        _rewrite_preview_manifest(
            manifest_path=manifest_path,
            label=label,
            extra_blockers=extra_blockers,
        )
    except GrokWrapperError as exc:
        _log("finalize_peer_session", "preview manifest rewrite failed: {}".format(exc))
        if primary_error is None:
            primary_error = exc

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
            "integrationReady": False,
            "preview": True,
        }
    }
    if result is not None and getattr(result, "answer", None):
        response["result"] = redact_secret_material(
            {"text": result.answer}, redact_keys=True
        )

    if primary_error is not None:
        from groklib.envelope import failure_envelope

        env = failure_envelope(
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
        _terminalize_peer_run(run_paths, env)
        return env

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
        "warnings": list(stage.acc.warnings or [])
        + [
            "peer-preview: not integration-ready; not eligible for /grok:handoff",
        ],
    }
    # Full GrokRunResult only (peer stop often uses a minimal stand-in Mock).
    if (
        result is not None
        and isinstance(getattr(result, "stderr", None), (str, type(None)))
        and all(hasattr(result, attr) for attr in ("parsed", "final_text", "structured"))
    ):
        try:
            grok_f, usage_f, response_f, extra_warnings = grok_usage_response_fields(result)
            fields["grok"] = grok_f
            fields["usage"] = usage_f
            if isinstance(response_f, dict):
                fields["response"] = {**response, **response_f}
            fields["warnings"] = list(fields.get("warnings") or []) + list(
                extra_warnings or []
            )
        except Exception as exc:
            _log("finalize_peer_session", "grok usage fields skipped: {}".format(exc))
    env = build_envelope(
        run_id=run_paths.run_id,
        mode="peer-stop",
        status="success",
        **fields,
    )
    _terminalize_peer_run(run_paths, env)
    return env
