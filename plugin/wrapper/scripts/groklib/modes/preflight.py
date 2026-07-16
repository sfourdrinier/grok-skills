# wrapper/scripts/groklib/modes/preflight.py
#
# `preflight` mode: a read-only readiness diagnostic that verifies, in order,
# every precondition a live Grok run depends on, emitting one progress event
# per check and recording the findings in the C4 envelope `response`. It never
# spawns a task-bearing Grok run; it only exercises the runnable CLI check, the
# auth material, a throwaway private home + login/inspect probes, the sandbox
# policy resolver, the platform advisory (D-SECRETREAD: secret reads are not
# denied, informational only), the state root permissions, and the stale-home
# audit. The FIRST failing check short-circuits to the matching classified
# failure envelope (fail closed).

import argparse
import os
import pathlib
import stat
import tempfile
from typing import Dict, List

from groklib import GrokWrapperError, grokcli, grokcli_probe, log_stderr, platformsupport, runstate, sandbox
from groklib.authhome import PrivateHome, create_private_home, destroy_private_home, render_config_toml
from groklib.envelope import build_envelope, failure_envelope
from groklib.progress import ProgressWriter
# C1/T4: reuse the single source in modes._shared instead of re-declaring these.
# ``source_grok_dir`` is imported under the module-private ``_source_grok_dir``
# name so the existing preflight test seam (which patches
# ``preflight._source_grok_dir``) keeps working while the path logic lives once
# in _shared.
from groklib.modes._shared import (
    AUTH_FILE_NAMES,
    resolve_binary as _resolve_binary,
    source_grok_dir as _source_grok_dir,
    terminalize_unexpected_failure,
    utc_now_iso as _utc_now_iso,
)

# A generous audit window so preflight's stale-home sweep never removes a home
# that a concurrent run legitimately owns; it only proves the sweep runs.
_STALE_HOME_MAX_AGE_SECONDS = 86400

_REQUESTED_MODEL = "grok-4.5"
_SANDBOX_MODES = ("review", "reason", "code", "verify")


def _log(function: str, message: str) -> None:
    log_stderr("modes.preflight", function, message)


def _make_private_home() -> PrivateHome:
    return create_private_home(
        source_grok_dir=_source_grok_dir(),
        auth_file_names=AUTH_FILE_NAMES,
        config_toml=render_config_toml(mode="preflight"),
    )


def _check_version(binary: pathlib.Path, progress: ProgressWriter, checks: List[dict]) -> None:
    if not binary.exists():
        raise GrokWrapperError(
            "tool-unavailable",
            "grok binary not found at {}".format(binary),
            {"binary": str(binary)},
        )
    version = grokcli.check_version(binary)
    progress.safe_emit("grok", "grok binary present and reports a version", data={"version": version})
    checks.append({"name": "grokVersion", "ok": True, "detail": version})


def _check_auth(progress: ProgressWriter, checks: List[dict]) -> None:
    grok_dir = _source_grok_dir()
    missing = [name for name in AUTH_FILE_NAMES if not (grok_dir / name).is_file()]
    if missing:
        raise GrokWrapperError(
            "auth-missing",
            "missing authentication file(s) in {}: {}".format(grok_dir, ", ".join(missing)),
            {"missingAuthFileNames": missing},
        )
    progress.safe_emit("authhome", "auth material present in source grok home", data={"authFileNames": list(AUTH_FILE_NAMES)})
    checks.append({"name": "authMaterial", "ok": True, "detail": "present under {}".format(grok_dir)})


def _check_home_and_login(
    binary: pathlib.Path,
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    checks: List[dict],
    probe_cleanup_holder: List[dict],
) -> None:
    home = _make_private_home()
    progress.safe_emit("authhome", "private home created", data={"home": str(home.home_dir)})
    try:
        leader_socket = runstate.allocate_leader_socket(home.home_dir, run_paths.run_id)
        login = grokcli_probe.probe_login(binary, home, leader_socket)
        models = login.get("models") or []
        if _REQUESTED_MODEL not in models:
            raise GrokWrapperError(
                "model-unavailable",
                "{} is not selectable in the private-home login probe".format(_REQUESTED_MODEL),
                {"models": list(models)},
            )
        progress.safe_emit(
            "grok", "probe_login: logged in, grok-4.5 selectable", data={"defaultModel": login.get("defaultModel")}
        )
        checks.append({"name": "login", "ok": True, "detail": "logged in; default {}".format(login.get("defaultModel"))})
    finally:
        destroy_result = destroy_private_home(home)
        # Round5 cleanup-outcome-lost-on-terminalize: publish the probe-home
        # teardown outcome so a RAW exception escaping this check (probe_login
        # raising a non-GrokWrapperError) still surfaces a FAILED teardown
        # fail-closed via run()'s terminalizer, not as not-applicable.
        probe_cleanup_holder[0] = destroy_result
        progress.safe_emit("cleanup", "private home destroyed", data={"cleanupStatus": destroy_result["status"]})

    if destroy_result["status"] != "clean":
        raise GrokWrapperError(
            "cleanup-failure",
            "private home could not be destroyed cleanly during preflight",
            {"cleanupStatus": destroy_result["status"]},
        )
    checks.append({"name": "privateHomeLifecycle", "ok": True, "detail": "created and destroyed clean"})


def _check_inspect(
    binary: pathlib.Path,
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    checks: List[dict],
    probe_cleanup_holder: List[dict],
) -> None:
    home = _make_private_home()
    try:
        leader_socket = runstate.allocate_leader_socket(home.home_dir, run_paths.run_id)
        surface = grokcli_probe.inspect_home(binary, home, leader_socket)
    finally:
        destroy_result = destroy_private_home(home)
        # Round5: publish the inspect-home teardown outcome for run()'s
        # terminalizer, so a RAW exception here surfaces a FAILED teardown
        # fail-closed rather than as not-applicable.
        probe_cleanup_holder[0] = destroy_result
        progress.safe_emit("cleanup", "inspect home destroyed", data={"cleanupStatus": destroy_result["status"]})

    # S2: an unclean inspect-home teardown is a classified failure, consistent
    # with the sibling _check_home_and_login. A leftover auth copy after the
    # probe is a spec-7 violation, never a passing readiness check.
    if destroy_result["status"] != "clean":
        raise GrokWrapperError(
            "cleanup-failure",
            "inspect private home could not be destroyed cleanly during preflight",
            {"cleanupStatus": destroy_result["status"]},
        )
    progress.safe_emit(
        "grok", "inspect_home: config surface present", data={"permissionsPresent": surface.get("permissions") is not None}
    )
    checks.append({"name": "inspectHome", "ok": True, "detail": "config surface returned"})


def _check_platform_probed(progress: ProgressWriter, checks: List[dict]) -> None:
    """Fail closed (probe-required) when this host has no captured Grok sandbox probe report.

    SEC1: preflight is a live-run readiness diagnostic, so an unprobed platform
    is NOT ready -- live modes are blocked there. Reuse the same gate the runners
    apply pre-spawn so preflight surfaces not-ready instead of a false green.
    """
    platformsupport.require_probed_platform_for_live()
    platform = platformsupport.current_platform()
    progress.safe_emit("sandbox", "platform has a captured Grok sandbox probe report", data={"platform": platform})
    checks.append({"name": "platformProbed", "ok": True, "detail": "probed platform {}".format(platform)})


def _check_sandbox_policies(progress: ProgressWriter, checks: List[dict]) -> None:
    private_tmp = pathlib.Path(tempfile.gettempdir()).resolve()
    synthetic_worktree = private_tmp / "preflight-sandbox-probe"
    profiles: Dict[str, str] = {}
    for mode in _SANDBOX_MODES:
        worktree = synthetic_worktree if mode in ("code", "verify") else None
        policy = sandbox.policy_for_mode(mode, worktree=worktree, private_tmp=private_tmp)
        profiles[mode] = policy.profile
    progress.safe_emit("sandbox", "write-confinement policy resolved for all modes", data={"profiles": profiles})
    detail = ", ".join("{}={}".format(mode, profiles[mode]) for mode in _SANDBOX_MODES)
    checks.append({"name": "sandboxPolicies", "ok": True, "detail": detail})


def _check_platform_advisory(progress: ProgressWriter, checks: List[dict]) -> None:
    platform = platformsupport.current_platform()
    probed = platform in platformsupport.PROBED_PLATFORMS
    progress.safe_emit(
        "sandbox",
        "secret-read denial advisory recorded (D-SECRETREAD)",
        data={"platform": platform, "platformProbed": probed},
    )
    # "secretReadDenial" is recorded as a check NAME (a value), never as a dict
    # key, so the C4 secret-key guard does not flag it; the informational
    # boolean lives under the guard-safe "value" key.
    checks.append(
        {
            "name": "secretReadDenial",
            "ok": True,
            "value": False,
            "detail": "advisory: D-SECRETREAD; sandbox confines writes only; secret reads are not denied (secretReadDenial=false)",
        }
    )


def _check_state_root(progress: ProgressWriter, checks: List[dict]) -> None:
    root = runstate.state_root()
    if not root.exists():
        raise GrokWrapperError(
            "validation-failure", "state root does not exist: {}".format(root), {"stateRoot": str(root)}
        )
    if not os.access(str(root), os.W_OK):
        raise GrokWrapperError(
            "validation-failure", "state root is not writable: {}".format(root), {"stateRoot": str(root)}
        )
    mode_detail = "n/a (non-posix)"
    if platformsupport.is_posix():
        actual_mode = stat.S_IMODE(os.stat(str(root)).st_mode)
        if actual_mode != 0o700:
            raise GrokWrapperError(
                "validation-failure",
                "state root mode is {:o}, expected 700".format(actual_mode),
                {"stateRoot": str(root), "mode": "{:o}".format(actual_mode)},
            )
        mode_detail = "0o700"
    progress.safe_emit("validate", "state root writable with correct permissions", data={"stateRoot": str(root)})
    checks.append({"name": "stateRootWritable", "ok": True, "detail": mode_detail})


def _check_stale_audit(progress: ProgressWriter, checks: List[dict]) -> None:
    removed = runstate.audit_stale_temp_homes(_STALE_HOME_MAX_AGE_SECONDS)
    progress.safe_emit("cleanup", "stale temp-home audit completed", data={"removedCount": len(removed)})
    checks.append({"name": "staleHomeAudit", "ok": True, "detail": "removed {} stale home(s)".format(len(removed))})


def _advance_preflight_running(run_paths: runstate.RunPaths) -> None:
    """Non-terminal: seed created → running; never terminalize without envelope."""
    record = runstate.load_run_record(run_paths.run_id)
    rev = int(record.get("recordRevision", 0))
    if record.get("lifecycle") == "created":
        record = runstate.set_lifecycle(run_paths, rev, "running")
        rev = int(record["recordRevision"])
    runstate.cas_update_run_record(
        run_paths,
        rev,
        {"requestedModel": _REQUESTED_MODEL, "status": "running"},
    )


def _persist_preflight_terminal(
    run_paths: runstate.RunPaths,
    envelope: dict,
    *,
    lifecycle: str,
) -> dict:
    """Envelope-first terminal persist for preflight (design §7.1). Fail closed."""
    from groklib.envelope import failure_envelope

    try:
        record = runstate.load_run_record(run_paths.run_id)
        rev = int(record.get("recordRevision", 0))
        life = record.get("lifecycle")
        if life == "created":
            record = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(record["recordRevision"])
            life = "running"
        if life == "running":
            record = runstate.set_lifecycle(run_paths, rev, "finalizing")
            rev = int(record["recordRevision"])
        runstate.persist_terminal_envelope(run_paths, rev, envelope, lifecycle=lifecycle)
        return envelope
    except Exception as exc:
        _log(
            "_persist_preflight_terminal",
            "could not persist terminal preflight envelope for {}: {}".format(run_paths.run_id, exc),
        )
        fail = failure_envelope(
            run_id=run_paths.run_id,
            mode="preflight",
            error_class="finalization-worker-missing-result",
            message="preflight terminal persist failed: {}".format(exc),
            detail={"runId": run_paths.run_id},
            progressStreamPath=str(run_paths.progress_path),
        )
        fail["doNotStore"] = True
        return fail


def run(args: argparse.Namespace) -> dict:
    """Run every preflight check in order and return a validated C4 envelope.

    Any UNCLASSIFIED exception escaping the checks or the final envelope build
    (e.g. an OSError or an InvalidEnvelopeError) is terminalized under the REAL
    run id via the shared ``terminalize_unexpected_failure`` helper, so the
    entrypoint never synthesizes a dangling run id and this run's run.json never
    stays stuck at status="running".
    """
    binary = _resolve_binary(args)
    run_paths = None
    progress = None
    # Round5 cleanup-outcome-lost-on-terminalize: the probe checks destroy their
    # private homes locally; this holder carries the last teardown outcome so a
    # RAW exception escaping to the terminalizer still surfaces a FAILED teardown
    # fail-closed instead of reporting not-applicable.
    probe_cleanup_holder: List[dict] = [{"status": "not-applicable", "detail": None}]
    try:
        # create_run is INSIDE the try (F1-create-run-outside-try) so a mid-create
        # failure terminalizes the REAL run rather than orphaning its on-disk dir.
        run_paths = runstate.create_run("preflight")
        progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        return _run_preflight_body(binary, run_paths, progress, probe_cleanup_holder)
    except BaseException as exc:  # last-resort: terminalize under the REAL run id
        # BaseException (not just Exception): a SIGTERM-driven SystemExit or a
        # KeyboardInterrupt still terminalizes and emits exactly one C4 envelope
        # (F5-sigterm-bypasses-envelope).
        paths = run_paths if run_paths is not None else getattr(exc, "run_paths", None)
        if paths is None:
            raise
        if progress is None:
            progress = ProgressWriter(paths.run_id, paths.progress_path)
        return terminalize_unexpected_failure(
            run_paths=paths,
            mode="preflight",
            progress=progress,
            exc=exc,
            write_terminal_record=lambda: None,
            log=_log,
            cleanup=probe_cleanup_holder[0],
        )


def _run_preflight_body(
    binary: pathlib.Path,
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    probe_cleanup_holder: List[dict],
) -> dict:
    """Run every preflight check in order and return a validated C4 envelope (classified path)."""
    try:
        _advance_preflight_running(run_paths)
    except Exception as exc:
        _log("_run_preflight_body", "could not advance to running: {}".format(exc))
    progress.safe_emit("start", "preflight run created", data={"mode": "preflight"})

    checks: List[dict] = []
    try:
        _check_version(binary, progress, checks)
        _check_auth(progress, checks)
        _check_platform_probed(progress, checks)
        _check_home_and_login(binary, run_paths, progress, checks, probe_cleanup_holder)
        _check_inspect(binary, run_paths, progress, checks, probe_cleanup_holder)
        _check_sandbox_policies(progress, checks)
        _check_platform_advisory(progress, checks)
        _check_state_root(progress, checks)
        _check_stale_audit(progress, checks)
    except GrokWrapperError as exc:
        _log("run", "preflight check failed: {} ({})".format(exc.error_class, exc))
        progress.safe_emit("done", "preflight failed: {}".format(exc.error_class), level="error")
        # F2 preflight-cleanup-field-on-classified-failure: thread the REAL probe-
        # home teardown outcome onto the top-level cleanup field, consistently with
        # the runners (modes/_shared._failure_envelope). Without this the classified
        # cleanup-failure path (a genuinely-failed auth-material teardown raised by
        # _check_home_and_login / _check_inspect) silently defaulted cleanup to
        # not-applicable, hiding the possibly-leaked auth copy from anything polling
        # cleanup.status. The holder carries the last probe teardown outcome (the
        # failed one on a cleanup-failure), or not-applicable if no probe ran yet.
        envelope = failure_envelope(
            run_id=run_paths.run_id,
            mode="preflight",
            error_class=exc.error_class,
            message=str(exc),
            detail=exc.detail or None,
            requestedModel=_REQUESTED_MODEL,
            progressStreamPath=str(run_paths.progress_path),
            response={"checks": checks},
            cleanup=probe_cleanup_holder[0],
        )
        return _persist_preflight_terminal(run_paths, envelope, lifecycle="failed")

    progress.safe_emit("done", "preflight complete", data={"checkCount": len(checks)})
    response = {
        "platform": platformsupport.current_platform(),
        "platformProbed": platformsupport.current_platform() in platformsupport.PROBED_PLATFORMS,
        "checks": checks,
    }
    # Wave 1: seed the short-lived version-keyed preflight cache on full success.
    version_detail = next(
        (c.get("detail") for c in checks if c.get("name") == "grokVersion" and c.get("ok")),
        None,
    )
    if isinstance(version_detail, str) and version_detail.strip():
        from groklib import preflight_cache

        preflight_cache.write_ok(version_detail)
    envelope = build_envelope(
        run_id=run_paths.run_id,
        mode="preflight",
        status="success",
        requestedModel=_REQUESTED_MODEL,
        progressStreamPath=str(run_paths.progress_path),
        response=response,
    )
    return _persist_preflight_terminal(run_paths, envelope, lifecycle="completed")
