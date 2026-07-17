# wrapper/scripts/groklib/modes/peer_finalize.py
#
# peer-stop finalization for the ACP peer channel. Runs the SAME ordered
# finalize as code/worktree (scopes + requiredValidation + build gate) via
# wrapper-executed commands. integration.ready is evidence-backed only - never
# forged, never from model claims. Split from peer.py for the 900-line cap.

from __future__ import annotations

import pathlib
import tempfile
from typing import Any, List, Optional

from groklib import GrokWrapperError, log_stderr, runstate
from groklib.authhome import PrivateHome, destroy_private_home
from groklib.code_handoff_finalize import code_handoff_finalize
from groklib.envelope import build_envelope, redact_secret_material
from groklib.implementation_handoff import (
    NO_AUTHORITATIVE_VALIDATION_KIND,
    enforce_ready_evidence_guard,
    write_manifest,
)
from groklib.modes._envelope import (
    AUTH_TEARDOWN_FAILED_ERROR_CLASS,
    grok_usage_response_fields,
)
from groklib.projectconfig import load_project_config
from groklib.sandbox import policy_for_mode, verify_enforcement
from groklib.worktree import ExternalWorktree


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer_finalize", function, message)


def _confinement_label(contract: Optional[dict]) -> str:
    """Same standing as code: contract-scopes when scopes present, else final-diff."""
    scopes = (contract or {}).get("writeScopes") if contract else None
    if isinstance(scopes, list) and len(scopes) > 0:
        return "contract-scopes"
    return "worktree-final-diff-only"


def _target_relative(peer_doc: dict) -> str:
    raw = peer_doc.get("targetRelative")
    if not isinstance(raw, str) or not raw.strip() or raw.strip() in (".", "./"):
        return ""
    return raw.strip()


def _maybe_verify_sandbox(
    *,
    home_path: pathlib.Path,
    worktree: ExternalWorktree,
) -> Optional[dict]:
    """Best-effort verify_enforcement at peer-stop; return a blocker on failure.

    ACP timing may leave no sandbox-events.jsonl; failure is recorded honestly
    as a soft not-ready blocker.
    """
    if not home_path.is_dir():
        return {
            "kind": "sandbox-failure",
            "message": "peer: private home missing at stop; sandbox not verified",
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
            "message": "peer sandbox verification failed: {}".format(exc),
            "detail": dict(exc.detail or {}, errorClass=exc.error_class),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "kind": "sandbox-failure",
            "message": "peer sandbox verification failed: {}".format(exc),
        }
    return None


def _apply_post_finalize_manifest(
    *,
    manifest_path: pathlib.Path,
    label: str,
    commands: List[dict],
    extra_blockers: Optional[List[dict]] = None,
) -> dict:
    """Set confinement, merge soft blockers, re-run the forgery guard, write."""
    import json

    if not manifest_path.is_file():
        raise GrokWrapperError(
            "artifact-generation-failure",
            "peer finalize produced no handoff manifest",
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

    # Confinement BEFORE write_manifest scan (never post-mutate after).
    doc["confinement"] = label
    integration = doc.get("integration")
    if not isinstance(integration, dict):
        integration = {"ready": False, "blockers": []}
        doc["integration"] = integration
    blockers: List[dict] = []
    raw_blockers = integration.get("blockers")
    if isinstance(raw_blockers, list):
        for item in raw_blockers:
            if isinstance(item, dict):
                blockers.append(dict(item))
    for extra in extra_blockers or []:
        if not isinstance(extra, dict):
            continue
        kind = extra.get("kind")
        if kind and any(b.get("kind") == kind for b in blockers):
            continue
        blockers.append(dict(extra))
    if blockers:
        # Soft extra blockers force not-ready (compute_integration_ready invariant).
        integration["ready"] = False
    integration["blockers"] = blockers
    # Re-assert anti-forgery after any post-processing (never allow ready without evidence).
    doc = enforce_ready_evidence_guard(doc, commands)
    write_manifest(manifest_path, doc)
    return doc


def _terminalize_peer_run(run_paths: runstate.RunPaths, envelope: dict) -> None:
    """Persist terminal envelope + lifecycle so cleanup can rebuild the worktree."""
    try:
        durable = dict(envelope)
        durable["mode"] = "peer-start"
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


def _build_gate_runner(
    stage: Any,
    *,
    peer_doc: dict,
    worktree: ExternalWorktree,
):
    """Return a run_build_gate callback that executes the real code-mode gate."""
    from groklib.modes import code as code_mod
    from groklib.modes import code_continue

    target_relative = _target_relative(peer_doc)
    repo_root = pathlib.Path(str(peer_doc.get("repoRoot") or worktree.repo_root))
    project_config = load_project_config(repo_root)
    package_manager = peer_doc.get("projectPackageManager")
    if package_manager is None:
        package_manager = project_config.package_manager
    elif package_manager == "":
        package_manager = None
    ws_name, pristine_scripts = code_continue.read_committed_manifest_fields_from_ref(
        worktree.path, worktree.base_revision, target_relative
    )
    pm_binary = code_mod._resolve_pm_binary()
    never_build = project_config.never_build_workspaces

    def _run_build_gate() -> None:
        code_mod._run_build_gate(
            stage,
            target_relative,
            package_manager,
            pm_binary,
            never_build,
            ws_name,
            pristine_scripts,
        )

    return _run_build_gate


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
    """Forensic finalize with REAL requiredValidation + build gate; evidence-backed ready.

    Returns a success (or classified failure) C4 envelope for peer-stop.
    exit_status comes only from real subprocess runs - nothing synthesizes exit 0.
    """
    from groklib import worktree_escape
    from groklib.modes import code as code_mod

    sentinel_name = peer_doc.get("sentinelName") or (".grok-run-" + run_paths.run_id)
    artifacts_dir = run_paths.run_dir / "artifacts"
    label = _confinement_label(contract)
    run_build_gate = _build_gate_runner(stage, peer_doc=peer_doc, worktree=worktree)

    try:
        handoff = code_handoff_finalize(
            stage=stage,
            sentinel_name=sentinel_name,
            contract=contract,  # FULL contract - requiredValidation executes
            artifacts_dir=artifacts_dir,
            original_baseline=original_baseline,
            run_build_gate=run_build_gate,
            assert_changes_within=worktree_escape.assert_changes_within,
            assert_original_checkout_unmodified=worktree_escape.assert_original_checkout_unmodified,
            assert_cwd_sentinel=code_mod._assert_cwd_sentinel,
            run_recorded_command=code_mod._run_recorded_command,
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
    commands = list(getattr(getattr(stage, "acc", None), "commands", None) or [])
    manifest_doc: Optional[dict] = None
    try:
        manifest_doc = _apply_post_finalize_manifest(
            manifest_path=manifest_path,
            label=label,
            commands=commands,
            extra_blockers=extra_blockers,
        )
    except GrokWrapperError as exc:
        _log("finalize_peer_session", "manifest post-process failed: {}".format(exc))
        if primary_error is None:
            primary_error = exc

    integration_ready = False
    if isinstance(manifest_doc, dict):
        integration_ready = bool(
            (manifest_doc.get("integration") or {}).get("ready") is True
        )
        # Defense in depth: ready without command evidence is a programming error.
        if integration_ready and not commands:
            integration_ready = False
            try:
                enforce_ready_evidence_guard(manifest_doc, commands)
                write_manifest(manifest_path, manifest_doc)
            except Exception as exc:
                _log(
                    "finalize_peer_session",
                    "forgery re-guard write failed: {}".format(exc),
                )

    # Destroy private home (auth material first via destroy_private_home).
    home = PrivateHome(
        home_dir=home_path,
        grok_dir=home_path / ".grok",
        config_path=home_path / ".grok" / "config.toml",
    )
    cleanup = destroy_private_home(home)

    # Fail closed when auth teardown could not be confirmed clean: a "failed"
    # cleanup means credential material may remain on disk, so peer-stop must NOT
    # report success (parity with the shared worktree lifecycle, which flips a
    # failed teardown to cleanup-failure).
    if (
        isinstance(cleanup, dict)
        and cleanup.get("status") == "failed"
        and primary_error is None
    ):
        primary_error = GrokWrapperError(
            AUTH_TEARDOWN_FAILED_ERROR_CLASS,
            "the peer private-home auth-material teardown could not be confirmed clean",
            {"reason": "auth-teardown-failed", "cleanupStatus": "failed"},
        )

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
            "integrationReady": integration_ready,
            "preview": False,
        }
    }
    if isinstance(manifest_doc, dict):
        response["integration"] = {
            "ready": integration_ready,
            "blockers": list(
                (manifest_doc.get("integration") or {}).get("blockers") or []
            ),
        }
        # Surface no-authoritative-validation clearly for operators.
        if not integration_ready:
            kinds = {
                b.get("kind")
                for b in (manifest_doc.get("integration") or {}).get("blockers") or []
                if isinstance(b, dict)
            }
            if NO_AUTHORITATIVE_VALIDATION_KIND in kinds:
                response["peer"]["notReadyReason"] = NO_AUTHORITATIVE_VALIDATION_KIND
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
            commands=list(stage.acc.commands or []),
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
        "warnings": list(stage.acc.warnings or []),
    }
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
