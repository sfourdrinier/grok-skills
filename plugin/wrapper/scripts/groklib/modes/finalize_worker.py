# wrapper/scripts/groklib/modes/finalize_worker.py
#
# Spawn-only finalization worker: loads a JSON payload, persists the terminal
# envelope via runstate.persist_terminal_envelope (envelope-first CAS), writes
# finalize-result.json. Parent owns progress and recovery when the worker is
# confirmed not alive (design §9 / §9.4).

from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import traceback
from typing import Any, Dict, Optional, Tuple

from groklib import log_stderr, runstate
from groklib import envelope as envelope_mod

_DEFAULT_BUDGETS = {
    "review": 120,
    "reason": 120,
    "code": 180,
    "verify": 180,
}
_TERMINATE_GRACE = 5.0
_KILL_GRACE = 5.0


def _log(function: str, message: str) -> None:
    log_stderr("modes.finalize_worker", function, message)


def finalize_budget_seconds(mode: str) -> int:
    env = os.environ.get("GROK_FINALIZE_TIMEOUT_SECONDS", "").strip()
    if env:
        try:
            value = int(env)
            return max(30, min(600, value))
        except ValueError:
            pass
    return int(_DEFAULT_BUDGETS.get(mode, 120))


def finalize_worker_main(payload_path: str) -> None:
    """Entry point for spawn Process(target=..., args=(payload_path,))."""
    path = pathlib.Path(payload_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log("finalize_worker_main", "bad payload: {}".format(exc))
        raise SystemExit(2) from exc

    run_id = payload["runId"]
    run_dir = pathlib.Path(payload["runDir"])
    paths = runstate.RunPaths(
        run_id=run_id,
        run_dir=run_dir,
        progress_path=run_dir / "progress.jsonl",
        envelope_path=run_dir / "envelope.json",
        trace_dir=run_dir / "trace",
    )
    result_path = pathlib.Path(payload["resultPath"])
    stderr_path = pathlib.Path(payload.get("stderrPath") or (run_dir / "finalize-worker.stderr"))
    expected = payload.get("expectedRecordRevision")
    envelope = payload.get("envelope")
    lifecycle = payload.get("lifecycle")

    try:
        record = runstate.persist_terminal_envelope(
            paths,
            int(expected) if expected is not None else None,
            envelope if isinstance(envelope, dict) else None,
            lifecycle=lifecycle if isinstance(lifecycle, str) else None,
        )
        result = {
            "schemaVersion": 1,
            "ok": True,
            "lifecycle": record.get("lifecycle"),
            "envelopePath": str(paths.envelope_path),
            "errorClass": None,
            "message": None,
            "recordRevisionAfter": record.get("recordRevision"),
        }
        runstate.write_json_atomic(result_path, result)
        raise SystemExit(0)
    except Exception as exc:
        try:
            stderr_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        error_class = getattr(exc, "error_class", None) or "cli-failure"
        result = {
            "schemaVersion": 1,
            "ok": False,
            "lifecycle": None,
            "envelopePath": None,
            "errorClass": error_class,
            "message": str(exc),
            "recordRevisionAfter": None,
        }
        try:
            runstate.write_json_atomic(result_path, result)
        except OSError:
            pass
        raise SystemExit(1) from exc


def run_finalize_parent(
    paths: runstate.RunPaths,
    *,
    mode: str,
    envelope: dict,
    lifecycle: str,
    expected_revision: int,
    progress: Any = None,
    budget_seconds: Optional[int] = None,
) -> Tuple[dict, Optional[str]]:
    """Spawn finalize worker; return (envelope_for_stdout, ephemeral_error_class_or_None).

    Parent durable recovery only when ``proc.is_alive() is False``.
    """
    budget = budget_seconds if budget_seconds is not None else finalize_budget_seconds(mode)
    payload_path = paths.run_dir / "finalize-payload.json"
    result_path = paths.run_dir / "finalize-result.json"
    stderr_path = paths.run_dir / "finalize-worker.stderr"
    payload = {
        "schemaVersion": 1,
        "runId": paths.run_id,
        "mode": mode,
        "runDir": str(paths.run_dir),
        "expectedRecordRevision": expected_revision,
        "resultPath": str(result_path),
        "stderrPath": str(stderr_path),
        "envelope": envelope,
        "lifecycle": lifecycle,
        "modeContext": {},
    }
    runstate.write_json_atomic(payload_path, payload)
    if progress is not None:
        progress.safe_emit("finalizing", "entering finalization")

    timed_out = False
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=finalize_worker_main,
        args=(str(payload_path),),
        name="grok-finalize",
    )
    proc.start()
    proc.join(timeout=float(budget))
    if proc.is_alive():
        timed_out = True
        proc.terminate()
        proc.join(_TERMINATE_GRACE)
    if proc.is_alive():
        proc.kill()
        proc.join(_KILL_GRACE)

    # Liveness gate: no durable parent write while worker alive
    if proc.is_alive():
        if progress is not None:
            progress.safe_emit("finalizing", "finalization worker unkillable", level="error")
        ephemeral = envelope_mod.failure_envelope(
            run_id=paths.run_id,
            mode=mode,
            error_class="finalization-worker-unkillable",
            message="finalization worker could not be terminated; no durable terminal write",
            detail={"runId": paths.run_id},
            progressStreamPath=str(paths.progress_path),
        )
        return ephemeral, "finalization-worker-unkillable"

    # Recovery under lock via persist API
    if paths.envelope_path.is_file():
        try:
            runstate.persist_terminal_envelope(paths, None, None)
            if progress is not None:
                progress.safe_emit("finalizing", "finalization succeeded")
            raw = json.loads(paths.envelope_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw, None
        except Exception as exc:
            _log("run_finalize_parent", "idempotent finish failed: {}".format(exc))

    exitcode = proc.exitcode
    if timed_out:
        fail = envelope_mod.failure_envelope(
            run_id=paths.run_id,
            mode=mode,
            error_class="finalization-timeout",
            message="finalization worker timed out",
            detail={"budgetSeconds": budget},
            progressStreamPath=str(paths.progress_path),
        )
        try:
            record = runstate.load_run_record(paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.persist_terminal_envelope(paths, rev, fail, lifecycle="failed")
        except Exception as exc:
            _log("run_finalize_parent", "timeout persist failed: {}".format(exc))
        if progress is not None:
            progress.safe_emit("finalizing", "finalization timed out", level="error")
        return fail, None

    if exitcode == 0 and paths.envelope_path.is_file():
        if progress is not None:
            progress.safe_emit("finalizing", "finalization succeeded")
        return json.loads(paths.envelope_path.read_text(encoding="utf-8")), None

    if exitcode == 0:
        fail = envelope_mod.failure_envelope(
            run_id=paths.run_id,
            mode=mode,
            error_class="finalization-worker-missing-result",
            message="finalization worker exited 0 without a terminal envelope",
            detail={"runId": paths.run_id},
            progressStreamPath=str(paths.progress_path),
        )
        try:
            record = runstate.load_run_record(paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.persist_terminal_envelope(paths, rev, fail, lifecycle="failed")
        except Exception as exc:
            _log("run_finalize_parent", "missing-result persist failed: {}".format(exc))
        return fail, None

    detail: Dict[str, Any] = {"exitCode": exitcode}
    if stderr_path.is_file():
        try:
            detail["stderrTail"] = stderr_path.read_text(encoding="utf-8")[-4000:]
        except OSError:
            pass
    fail = envelope_mod.failure_envelope(
        run_id=paths.run_id,
        mode=mode,
        error_class="cli-failure",
        message="finalization worker exited nonzero without a terminal envelope",
        detail=detail,
        progressStreamPath=str(paths.progress_path),
    )
    try:
        record = runstate.load_run_record(paths.run_id)
        rev = int(record.get("recordRevision", 0))
        runstate.persist_terminal_envelope(paths, rev, fail, lifecycle="failed")
    except Exception as exc:
        _log("run_finalize_parent", "cli-failure persist failed: {}".format(exc))
    return fail, None
