# wrapper/scripts/groklib/modes/finalize_worker.py
#
# Spawn finalization worker: loads JSON payload, persists terminal envelope via
# runstate.persist_terminal_envelope (envelope-first CAS), writes finalize-result.
# Parent owns progress and recovery only when the worker is confirmed not alive
# (design §9 / §9.4).
#
# Note (locked PR1 deviation): envelope assembly remains in the parent process;
# the worker owns durable terminal persist under a hang budget. Design §9 full
# post-Grok finalize ownership is a future tighten, not silent scope creep.

from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import traceback
from typing import Any, Dict, Optional, Tuple

from groklib import log_stderr, runstate
from groklib import envelope as envelope_mod
from groklib import platformsupport

_DEFAULT_BUDGETS = {
    "review": 120,
    "reason": 120,
    "code": 180,
    "verify": 180,
}
_TERMINATE_GRACE = 5.0
_KILL_GRACE = 5.0
_FILE_MODE = 0o600


def _log(function: str, message: str) -> None:
    log_stderr("modes.finalize_worker", function, message)


def _write_private_text(path: pathlib.Path, text: str) -> None:
    """Atomic private text write (temp sibling + replace) so readers never see empty O_TRUNC."""
    parent = path.parent
    tmp_path = parent / "{}.tmp.{}".format(path.name, os.getpid())
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        platformsupport.restrict_file_permissions(tmp_path)
        os.replace(str(tmp_path), str(path))
        platformsupport.restrict_file_permissions(path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _abandon_process_for_shutdown(proc: Any) -> None:
    """Stop interpreter exit from joining a still-alive non-daemon child (unkillable path)."""
    try:
        import multiprocessing.process as mp_process

        children = getattr(mp_process, "_children", None)
        if children is not None:
            children.discard(proc)
    except Exception as exc:
        _log("_abandon_process_for_shutdown", "could not abandon process: {}".format(exc))


def finalize_budget_seconds(mode: str) -> int:
    env = os.environ.get("GROK_FINALIZE_TIMEOUT_SECONDS", "").strip()
    if env:
        try:
            value = int(env)
            return max(30, min(600, value))
        except ValueError:
            pass
    return int(_DEFAULT_BUDGETS.get(mode, 120))


# Liveness file content "starting" = parent is about to spawn / mid-start (no pid yet).
_WORKER_STARTING_TOKEN = "starting"


def _worker_pid_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "finalize-worker.pid"


def mark_worker_starting(run_dir: pathlib.Path) -> None:
    """Gate durable parent terminalization before proc.start() obtains a real pid."""
    try:
        _write_private_text(_worker_pid_path(run_dir), _WORKER_STARTING_TOKEN)
    except OSError as exc:
        _log("mark_worker_starting", "could not write finalize-worker.pid: {}".format(exc))


def write_worker_pid(run_dir: pathlib.Path, pid: int) -> None:
    """Record the live finalize worker pid so SIGTERM recovery can observe liveness."""
    try:
        _write_private_text(_worker_pid_path(run_dir), str(int(pid)))
    except OSError as exc:
        _log("write_worker_pid", "could not write finalize-worker.pid: {}".format(exc))


def clear_worker_pid(run_dir: pathlib.Path) -> None:
    try:
        path = _worker_pid_path(run_dir)
        if path.is_file():
            path.unlink()
    except OSError as exc:
        _log("clear_worker_pid", "could not remove finalize-worker.pid: {}".format(exc))


def finalize_worker_is_alive(run_dir: pathlib.Path) -> bool:
    """True if finalize is in progress (starting or live pid) — design §9.4 gate.

    The ``starting`` token closes the race between ``proc.start()`` and writing
    the real pid: SIGTERM in that window must not durable-cancel while a worker
    may already be running.
    """
    path = _worker_pid_path(run_dir)
    if not path.is_file():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if raw == _WORKER_STARTING_TOKEN:
        return True
    try:
        pid = int(raw)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive (fail closed).
        return True
    except OSError:
        return False
    return True


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
    # Fail closed if runDir is not under our state root
    try:
        run_dir.resolve().relative_to(runstate.state_root().resolve())
    except ValueError as exc:
        _log("finalize_worker_main", "runDir outside state root: {}".format(run_dir))
        raise SystemExit(2) from exc

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
        runstate.verify_owner_marker(paths.run_dir / "owner.json")
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
            _write_private_text(stderr_path, traceback.format_exc())
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


def _load_valid_envelope(paths: runstate.RunPaths) -> Optional[dict]:
    if not paths.envelope_path.is_file():
        return None
    try:
        raw = json.loads(paths.envelope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if envelope_mod.validate_envelope(raw):
        return None
    return raw


def run_finalize_parent(
    paths: runstate.RunPaths,
    *,
    mode: str,
    envelope: dict,
    lifecycle: str,
    expected_revision: int,
    progress: Any = None,
    budget_seconds: Optional[int] = None,
) -> Tuple[dict, Optional[str], bool]:
    """Spawn finalize worker.

    Returns ``(envelope_for_stdout, ephemeral_error_class_or_None, durable_ok)``.
    When ``durable_ok`` is False, the entrypoint must not store envelope.json.
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
    # Liveness marker BEFORE start so SIGTERM cannot durable-cancel in the
    # start→pid-write window (worker may already be running).
    mark_worker_starting(paths.run_dir)
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=finalize_worker_main,
        args=(str(payload_path),),
        name="grok-finalize",
    )
    # Non-daemon: if parent is cancelled (SIGTERM) while waiting, the worker must
    # keep running to finish durable envelope persist. Unkillable path abandons
    # the child from the process join set so interpreter exit does not hang.
    proc.daemon = False
    try:
        proc.start()
    except Exception as spawn_exc:
        clear_worker_pid(paths.run_dir)
        _log("run_finalize_parent", "finalize worker spawn failed: {}".format(spawn_exc))
        fail = envelope_mod.failure_envelope(
            run_id=paths.run_id,
            mode=mode,
            error_class="cli-failure",
            message="finalize worker could not be started: {}".format(spawn_exc),
            detail={"runId": paths.run_id, "reason": "finalize-spawn-failed"},
            progressStreamPath=str(paths.progress_path),
        )
        try:
            record = runstate.load_run_record(paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.persist_terminal_envelope(paths, rev, fail, lifecycle="failed")
        except Exception as persist_exc:
            _log("run_finalize_parent", "spawn-failure persist failed: {}".format(persist_exc))
            fail["doNotStore"] = True
            return fail, None, False
        if progress is not None:
            progress.safe_emit("finalizing", "finalization worker spawn failed", level="error")
        return fail, None, True

    worker_pid = getattr(proc, "pid", None)
    if worker_pid is not None:
        write_worker_pid(paths.run_dir, int(worker_pid))
    try:
        try:
            proc.join(timeout=float(budget))
            if proc.is_alive():
                timed_out = True
                proc.terminate()
                proc.join(_TERMINATE_GRACE)
            if proc.is_alive():
                proc.kill()
                proc.join(_KILL_GRACE)

            if proc.is_alive():
                if progress is not None:
                    progress.safe_emit(
                        "finalizing", "finalization worker unkillable", level="error"
                    )
                ephemeral = envelope_mod.failure_envelope(
                    run_id=paths.run_id,
                    mode=mode,
                    error_class="finalization-worker-unkillable",
                    message="finalization worker could not be terminated; no durable terminal write",
                    detail={"runId": paths.run_id},
                    progressStreamPath=str(paths.progress_path),
                )
                # doNotStore: entrypoint must not write envelope.json
                ephemeral["doNotStore"] = True
                # Keep pid file while returning so concurrent terminalize still sees
                # the worker as live. Abandon join-on-exit so a stuck child cannot
                # hang CLI shutdown (worker stays non-daemon for cancel/success path).
                _abandon_process_for_shutdown(proc)
                return ephemeral, "finalization-worker-unkillable", False
        except BaseException:
            # SIGTERM/KeyboardInterrupt during join: abandon non-daemon child so
            # interpreter shutdown does not hang after ephemeral doNotStore cancel.
            try:
                if proc.is_alive():
                    _abandon_process_for_shutdown(proc)
            except Exception as abandon_exc:
                _log(
                    "run_finalize_parent",
                    "abandon after join interrupt failed: {}".format(abandon_exc),
                )
            raise
    finally:
        # Keep pid while still alive (unkillable / interrupted); clear once dead.
        try:
            if not proc.is_alive():
                clear_worker_pid(paths.run_dir)
        except Exception:
            pass

    # Recovery under lock semantics via persist API
    existing = _load_valid_envelope(paths)
    if existing is not None:
        try:
            runstate.persist_terminal_envelope(paths, None, None)
        except Exception as exc:
            _log("run_finalize_parent", "idempotent finish failed: {}".format(exc))
        # Always return the durable envelope, never a synthetic timeout
        if progress is not None:
            if existing.get("status") == "success":
                progress.safe_emit("finalizing", "finalization succeeded")
            else:
                progress.safe_emit("finalizing", "finalization finished with durable failure")
        return existing, None, True

    exitcode = proc.exitcode
    result_meta: Dict[str, Any] = {}
    if result_path.is_file():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result_meta = loaded
        except (OSError, json.JSONDecodeError):
            pass

    if exitcode == 0:
        # exit 0 but no valid envelope
        fail = envelope_mod.failure_envelope(
            run_id=paths.run_id,
            mode=mode,
            error_class="finalization-worker-missing-result",
            message="finalization worker exited 0 without a terminal envelope",
            detail={"runId": paths.run_id, "workerResult": result_meta or None},
            progressStreamPath=str(paths.progress_path),
        )
        try:
            record = runstate.load_run_record(paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.persist_terminal_envelope(paths, rev, fail, lifecycle="failed")
        except Exception as exc:
            _log("run_finalize_parent", "missing-result persist failed: {}".format(exc))
            fail["doNotStore"] = True
            return fail, None, False
        return fail, None, True

    if timed_out:
        fail = envelope_mod.failure_envelope(
            run_id=paths.run_id,
            mode=mode,
            error_class="finalization-timeout",
            message="finalization worker timed out",
            detail={"budgetSeconds": budget, "workerResult": result_meta or None},
            progressStreamPath=str(paths.progress_path),
        )
        try:
            record = runstate.load_run_record(paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.persist_terminal_envelope(paths, rev, fail, lifecycle="failed")
        except Exception as exc:
            _log("run_finalize_parent", "timeout persist failed: {}".format(exc))
            fail["doNotStore"] = True
            if progress is not None:
                progress.safe_emit("finalizing", "finalization timed out", level="error")
            return fail, None, False
        if progress is not None:
            progress.safe_emit("finalizing", "finalization timed out", level="error")
        return fail, None, True

    detail: Dict[str, Any] = {"exitCode": exitcode, "workerResult": result_meta or None}
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
        fail["doNotStore"] = True
        return fail, None, False
    return fail, None, True
