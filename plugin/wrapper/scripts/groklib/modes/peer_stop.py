# wrapper/scripts/groklib/modes/peer_stop.py
#
# Peer-stop lifecycle owner: peer.json running -> stopping -> stopped|failed under
# run_lock with stopOwner identity, durable terminal envelope idempotency
# (run.json/envelope.json remain run_lifecycle SSOT), abandoned-stopping reclaim
# after bounded grace, empty stop-stage builders (instance fields, never shared
# class mutables), resident stop_session, and safe fallback when the control
# socket is gone (verify wrapper pid/startToken before local finalize).

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
from typing import Any, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, platformsupport, runstate
from groklib.envelope import validate_envelope
from groklib.modes import peer_control
from groklib.modes.peer_process import kill_recorded_child, unregister_active_child
from groklib.peer_doc import (
    TERMINAL_PEER_LIFECYCLES as _TERMINAL_PEER_LIFECYCLES,
    mutate_peer_doc,
    read_peer_doc_unlocked,
    sync_prompts_handled_for_finalize,
    write_peer_doc_unlocked,
)
from groklib.progress import ProgressWriter

_PEER_STOP_CLAIM_POLL_SECONDS = 0.05
_PEER_STOP_CLAIM_WAIT_SECONDS = 30.0
# Grace before reclaiming a stopping claim whose owner is dead/unverified.
_PEER_STOP_OWNER_GRACE_SECONDS = 2.0
_CLAIMABLE_PEER_LIFECYCLES = frozenset({"running", "died", None})


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer_stop", function, message)


def build_empty_stop_stage(
    *,
    run_id: str,
    worktree_path: pathlib.Path,
    session_id: Optional[str],
    progress: Any,
    worktree: Any = None,
) -> Any:
    """Fresh stop stage with instance-owned lists (never shared class mutables)."""

    class _Acc:
        def __init__(self) -> None:
            self.commands: List[dict] = []
            self.changed_files: List[str] = []
            self.diff_summary: Optional[str] = None
            self.effective_working_directory = str(worktree_path)
            self.warnings: List[str] = []
            self.verifier = None

    class _Stage:
        pass

    stage = _Stage()
    stage.worktree = worktree
    stage.run_id = run_id
    stage.acc = _Acc()
    stage.progress = progress
    stage.result = type(
        "R",
        (),
        {
            "answer": "",
            "session_id": session_id,
            "request_id": None,
            "stop_reason": "cancelled",
            "model_usage": None,
            "turns": None,
            "raw_usage": None,
            "parsed": {},
            "stderr": "",
        },
    )()
    return stage


def load_durable_terminal_envelope(run_paths: runstate.RunPaths) -> Optional[dict]:
    """Return a valid durable terminal envelope when present (run_lifecycle SSOT).

    Does not revalidate sandbox/home or mutate state. Mode may be peer-start
    (resident terminalize rewrites mode for run-record binding).
    """
    path = run_paths.envelope_path
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if validate_envelope(raw):
        return None
    if raw.get("runId") != run_paths.run_id:
        return None
    status = raw.get("status")
    if status not in ("success", "failure"):
        return None
    return raw


def _current_stop_owner() -> dict:
    pid = os.getpid()
    token = platformsupport.process_start_token(pid)
    return {
        "pid": pid,
        "startToken": token if isinstance(token, str) else "",
        "claimedAt": time.time(),
    }


def _stop_owner_is_live(owner: Any) -> bool:
    """True when stopOwner must be treated as still holding the claim.

    Fail closed:
      - missing/empty/unavailable startToken while PID is alive => live (do not steal)
      - unreadable current token while PID is alive => live
      - exception during probe => live
    PID reuse with a *wrong* non-empty stored token is not live (safe reclaim).
    """
    if not isinstance(owner, dict):
        return False
    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    try:
        if not platformsupport.process_is_alive(pid):
            return False
    except Exception:
        return True
    token = owner.get("startToken")
    # Alive PID with missing/empty token: never treat as safely dead.
    if not isinstance(token, str) or not token:
        return True
    try:
        current = platformsupport.process_start_token(pid)
        if current is None:
            # Alive but token unreadable: treat as possibly live (never steal).
            return True
        return current == token
    except Exception:
        # Fail closed: unknown liveness means do not reclaim.
        return True


def _owner_claim_age_seconds(owner: Any) -> float:
    if not isinstance(owner, dict):
        return float("inf")
    claimed = owner.get("claimedAt")
    if not isinstance(claimed, (int, float)):
        return float("inf")
    return max(0.0, time.time() - float(claimed))


def _can_reclaim_stopping(doc: dict) -> bool:
    """True when stopping has no live verified owner after grace (or no owner)."""
    owner = doc.get("stopOwner")
    if _stop_owner_is_live(owner):
        return False
    # Missing/malformed owner is abandoned; reclaim after grace from claim time
    # when present, else immediately (poisoned forever without evidence).
    age = _owner_claim_age_seconds(owner)
    if owner is None or not isinstance(owner, dict):
        return True
    return age >= float(_PEER_STOP_OWNER_GRACE_SECONDS)


def _apply_lifecycle_patch(
    doc: dict,
    *,
    lifecycle: str,
    stop_owner: Optional[dict],
    clear_stop_owner: bool,
) -> dict:
    """Patch lifecycle/stop ownership on a re-read copy (preserve other fields)."""
    patched = dict(doc)
    patched["lifecycle"] = lifecycle
    if clear_stop_owner:
        patched.pop("stopOwner", None)
    elif stop_owner is not None:
        patched["stopOwner"] = dict(stop_owner)
    return patched


def mark_peer_lifecycle(
    run_paths: runstate.RunPaths,
    lifecycle: str,
    *,
    base_doc: Optional[dict] = None,
) -> dict:
    """Atomically set peer.json lifecycle under run_lock (peer channel owner).

    Always re-reads under the lock and patches only lifecycle/stop ownership
    fields. ``base_doc`` is accepted for call-site compatibility but never
    written as a whole document (prevents clobbering concurrent field updates).
    """
    del base_doc  # never trust a stale whole-doc write
    clear_owner = lifecycle in _TERMINAL_PEER_LIFECYCLES

    def _mutator(doc: dict) -> dict:
        return _apply_lifecycle_patch(
            doc,
            lifecycle=lifecycle,
            stop_owner=None,
            clear_stop_owner=clear_owner,
        )

    return mutate_peer_doc(run_paths, _mutator)


def claim_peer_stop(
    run_paths: runstate.RunPaths,
) -> Tuple[str, dict, Optional[dict]]:
    """Claim exclusive local finalize or return durable terminal.

    Returns ``(outcome, peer_doc, durable_env)`` where outcome is:
      - ``terminal``: durable envelope exists or peer already terminal
      - ``claimed``: this caller owns stopping; must finalize
      - ``wait``: another live stopper holds stopping (caller should poll)
    """
    # Claim must re-read + patch under one lock with durable-envelope check.
    with runstate.run_lock(run_paths):
        durable = load_durable_terminal_envelope(run_paths)
        doc = read_peer_doc_unlocked(run_paths)
        if durable is not None:
            life = doc.get("lifecycle")
            if life not in _TERMINAL_PEER_LIFECYCLES:
                terminal_life = (
                    "stopped" if durable.get("status") == "success" else "failed"
                )
                doc = _apply_lifecycle_patch(
                    doc,
                    lifecycle=terminal_life,
                    stop_owner=None,
                    clear_stop_owner=True,
                )
                write_peer_doc_unlocked(run_paths, doc)
            return "terminal", doc, durable
        life = doc.get("lifecycle")
        if life in _TERMINAL_PEER_LIFECYCLES:
            return "terminal", doc, None
        if life == "stopping":
            if not _can_reclaim_stopping(doc):
                return "wait", doc, None
            # Abandoned / dead owner after grace: reclaim atomically.
            _log(
                "claim_peer_stop",
                "reclaiming abandoned stopping claim for run {}".format(
                    run_paths.run_id
                ),
            )
        elif life not in _CLAIMABLE_PEER_LIFECYCLES and life is not None:
            # Unknown non-terminal values still claim (fail closed toward stop).
            _log(
                "claim_peer_stop",
                "claiming stop from unexpected lifecycle {!r}".format(life),
            )
        owner = _current_stop_owner()
        doc = _apply_lifecycle_patch(
            doc,
            lifecycle="stopping",
            stop_owner=owner,
            clear_stop_owner=False,
        )
        write_peer_doc_unlocked(run_paths, doc)
        return "claimed", doc, None


def wait_for_peer_stop_terminal(
    run_paths: runstate.RunPaths,
    *,
    timeout: float = _PEER_STOP_CLAIM_WAIT_SECONDS,
) -> Tuple[str, Optional[dict], Optional[dict]]:
    """Poll while another stopper holds the claim.

    Returns ``(outcome, peer_doc_or_none, durable_or_none)``:
      - terminal + durable
      - claimed (previous stopper abandoned; this caller owns finalize)
      - timeout (still stopping / no durable)
    """
    deadline = time.time() + max(0.1, float(timeout))
    last_doc: Optional[dict] = None
    while time.time() < deadline:
        durable = load_durable_terminal_envelope(run_paths)
        if durable is not None:
            return "terminal", last_doc, durable
        outcome, doc, durable2 = claim_peer_stop(run_paths)
        last_doc = doc
        if outcome == "terminal":
            return "terminal", doc, durable2
        if outcome == "claimed":
            return "claimed", doc, None
        time.sleep(_PEER_STOP_CLAIM_POLL_SECONDS)
    durable = load_durable_terminal_envelope(run_paths)
    if durable is not None:
        return "terminal", last_doc, durable
    return "timeout", last_doc, None


def kill_recorded_wrapper(doc: dict) -> bool:
    """Kill peer.json-recorded resident wrapper only on confirmed pid+startToken.

    Returns True when a kill was attempted on a positively-confirmed live wrapper.
    Never kills a recycled pid or this process. Best-effort; never raises.
    """
    wrapper = doc.get("wrapper") or {}
    wrapper_pid = wrapper.get("pid")
    wrapper_token = wrapper.get("startToken")
    if not isinstance(wrapper_pid, int) or not isinstance(wrapper_token, str) or not wrapper_token:
        return False
    try:
        if wrapper_pid == os.getpid():
            return False
        if not platformsupport.process_is_alive(wrapper_pid):
            return False
        current = platformsupport.process_start_token(wrapper_pid)
        if current is None or current != wrapper_token:
            return False
        if platformsupport.is_posix():
            try:
                if os.getpgid(wrapper_pid) == os.getpgid(0):
                    # Same process group as this peer-stop process: refuse group kill.
                    return False
            except OSError:
                # Pid may be synthetic in tests or already exiting; still attempt
                # pid-tree kill after identity was positively confirmed above.
                pass
        platformsupport.kill_process_tree_by_pid(wrapper_pid)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        _log("kill_recorded_wrapper", "could not kill wrapper {}: {}".format(wrapper_pid, exc))
        return False


def wrapper_still_live(doc: dict) -> bool:
    """True when peer.json wrapper pid must be treated as a live resident.

    Fail closed for fallback finalize/kill:
      - missing/empty startToken while PID is alive => live (never finalize/kill)
      - unreadable current token while PID is alive => live
      - exception during probe => live
    The current process is never treated as a foreign resident (peer-stop client
    fixtures and mis-records may reuse this pid; local finalize is owned here).
    """
    wrapper = doc.get("wrapper") or {}
    wrapper_pid = wrapper.get("pid")
    if not isinstance(wrapper_pid, int) or isinstance(wrapper_pid, bool):
        return False
    try:
        if wrapper_pid == os.getpid():
            return False
        if not platformsupport.process_is_alive(wrapper_pid):
            return False
    except Exception:
        return True
    wrapper_token = wrapper.get("startToken")
    # Alive PID with missing/empty recorded token: cannot positively prove dead.
    if not isinstance(wrapper_token, str) or not wrapper_token:
        return True
    try:
        current = platformsupport.process_start_token(wrapper_pid)
        if current is None:
            # Alive but token unreadable: treat as possibly live (fail closed).
            return True
        return current == wrapper_token
    except Exception:
        return True


def ensure_wrapper_down_for_fallback(doc: dict) -> None:
    """Before local finalize: terminate confirmed resident or refuse if still live."""
    if not wrapper_still_live(doc):
        return
    kill_recorded_wrapper(doc)
    if wrapper_still_live(doc):
        time.sleep(0.05)
        kill_recorded_wrapper(doc)
    if wrapper_still_live(doc):
        raise GrokWrapperError(
            "acp-failure",
            "peer-stop control failed while resident wrapper is still live; "
            "refusing local finalize (would race the serving process)",
            {
                "hint": "wrapper-still-live",
                "wrapperPid": (doc.get("wrapper") or {}).get("pid"),
            },
        )


def _return_or_claim_after_wait(
    run_paths: runstate.RunPaths,
    *,
    run_id: str,
) -> Tuple[str, dict, Optional[dict]]:
    waited_outcome, waited_doc, waited_env = wait_for_peer_stop_terminal(run_paths)
    if waited_outcome == "terminal" and waited_env is not None:
        return "terminal", waited_doc or {}, waited_env
    if waited_outcome == "claimed" and waited_doc is not None:
        return "claimed", waited_doc, None
    if waited_outcome == "terminal" and waited_env is None:
        raise GrokWrapperError(
            "acp-failure",
            "peer session already terminal without durable envelope; refusing re-finalize",
            {"runId": run_id},
        )
    raise GrokWrapperError(
        "acp-failure",
        "peer-stop timed out waiting for concurrent finalize",
        {"runId": run_id},
    )


def _lifecycle_for_envelope(env: dict) -> str:
    return "stopped" if env.get("status") == "success" else "failed"


def _persist_minimal_terminal_failure(
    run_paths: runstate.RunPaths,
    *,
    message: str,
    detail: Optional[dict] = None,
) -> Optional[dict]:
    """Best-effort durable failure envelope so reclaim does not re-finalize forever."""
    from groklib.envelope import failure_envelope
    from groklib.modes import peer_finalize

    try:
        env = failure_envelope(
            run_id=run_paths.run_id,
            mode="peer-stop",
            error_class="acp-failure",
            message=message,
            detail=detail or {"reason": "finalize-exception-no-durable"},
            progressStreamPath=str(run_paths.progress_path),
        )
        if peer_finalize._terminalize_peer_run(run_paths, env):
            return env
    except Exception as exc:
        _log(
            "_persist_minimal_terminal_failure",
            "could not persist minimal failure: {}".format(exc),
        )
    return None


def _mark_terminal_if_durable(
    run_paths: runstate.RunPaths,
    env: dict,
) -> Tuple[bool, Optional[dict]]:
    """Mark stopped/failed only when a durable terminal envelope is on disk.

    Returns ``(marked, durable_or_none)``. Never trusts the in-memory env alone:
    a success/failure return without durable evidence must leave reclaimable
    stopping ownership (or a separately-persisted minimal failure).
    """
    durable = load_durable_terminal_envelope(run_paths)
    if durable is None:
        return False, None
    try:
        mark_peer_lifecycle(run_paths, _lifecycle_for_envelope(durable))
        return True, durable
    except Exception as exc:
        _log("_mark_terminal_if_durable", "post-durable lifecycle mark: {}".format(exc))
        return False, durable


def _finalize_and_mark(
    *,
    run_paths: runstate.RunPaths,
    peer_doc: dict,
    home_path: pathlib.Path,
    worktree: Any,
    contract: Optional[dict],
    original_baseline: Any,
    stage: Any,
) -> dict:
    from groklib.modes import peer_finalize

    # Sentinel enforcement uses max(safe disk, in-memory) under lock so a stale
    # lower resident counter cannot skip the cwd proof after a successful prompt.
    try:
        peer_doc = sync_prompts_handled_for_finalize(run_paths, peer_doc)
    except Exception as exc:
        _log(
            "_finalize_and_mark",
            "promptsHandled sync before finalize: {}".format(exc),
        )

    try:
        env = peer_finalize.finalize_peer_session(
            run_paths=run_paths,
            peer_doc=peer_doc,
            home_path=home_path,
            worktree=worktree,
            contract=contract,
            original_baseline=original_baseline,
            stage=stage,
        )
    except Exception as exc:
        # Exception/no durable: keep reclaimable stopping owner, or persist a
        # minimal durable failure and mark failed under lock when possible.
        durable = load_durable_terminal_envelope(run_paths)
        if durable is None:
            durable = _persist_minimal_terminal_failure(
                run_paths,
                message="peer finalize raised before durable terminal envelope: {}".format(
                    exc
                ),
                detail={
                    "reason": "finalize-exception",
                    "errorType": type(exc).__name__,
                },
            )
        if durable is not None:
            try:
                mark_peer_lifecycle(run_paths, _lifecycle_for_envelope(durable))
            except Exception as mark_exc:
                _log(
                    "_finalize_and_mark",
                    "could not mark failed after exception: {}".format(mark_exc),
                )
        # Leave lifecycle=stopping + stopOwner when no durable could be written so
        # a later stopper can reclaim after grace (never poison as terminal).
        raise
    marked, durable = _mark_terminal_if_durable(run_paths, env)
    if durable is not None:
        # Durable terminal envelope is SSOT. Lifecycle mark is best-effort alignment
        # under lock; if mark_peer_lifecycle fails after durable success/failure, still
        # return the durable envelope and leave reclaimable stopping for later align.
        # Never invent a missing-durable failure when durable evidence exists.
        if not marked:
            _log(
                "_finalize_and_mark",
                "durable terminal present but lifecycle mark failed; returning durable",
            )
        return durable
    # Finalize returned an envelope but durable evidence is missing: do NOT mark
    # peer.json terminal. Prefer returning a failure shape so callers do not treat
    # undurable success as complete; keep stopping reclaimable.
    if env.get("status") == "success":
        _log(
            "_finalize_and_mark",
            "finalize returned success without durable envelope; refusing terminal mark",
        )
        from groklib.envelope import failure_envelope

        return failure_envelope(
            run_id=run_paths.run_id,
            mode="peer-stop",
            error_class="state-ownership-violation",
            message="peer-stop finalize lacked durable terminal evidence; leaving reclaimable stopping",
            detail={"reason": "terminalize-missing-after-finalize"},
            progressStreamPath=str(run_paths.progress_path),
            response=env.get("response") if isinstance(env.get("response"), dict) else None,
        )
    return env


def stop_session(session: Any) -> dict:
    """session/cancel, tear down child, code finalize path, destroy home."""
    run_paths = session.run_paths
    durable = load_durable_terminal_envelope(run_paths)
    if durable is not None:
        return durable
    outcome, claimed_doc, durable2 = claim_peer_stop(run_paths)
    if outcome == "terminal" and durable2 is not None:
        return durable2
    if outcome == "terminal" and durable2 is None:
        raise GrokWrapperError(
            "acp-failure",
            "peer session already terminal without durable envelope; refusing re-finalize",
            {"runId": session.run_id},
        )
    if outcome == "wait":
        outcome, claimed_doc, durable2 = _return_or_claim_after_wait(
            run_paths, run_id=session.run_id
        )
        if outcome == "terminal" and durable2 is not None:
            return durable2
    session.peer_doc = claimed_doc

    try:
        session.acp.session_cancel(session_id=session.session_id)
    except Exception as exc:
        _log("stop_session", "cancel: {}".format(exc))
    try:
        session.acp.close()
    except Exception as exc:
        _log("stop_session", "acp close: {}".format(exc))
    child = session.child
    if child is not None:
        try:
            platformsupport.kill_process_tree(child)
        except Exception as exc:
            _log("stop_session", "kill child: {}".format(exc))
        try:
            child.wait(timeout=5)
        except Exception:
            pass
        try:
            unregister_active_child(child)
        except Exception as exc:
            _log("stop_session", "unregister active child: {}".format(exc))

    stage = build_empty_stop_stage(
        run_id=session.run_id,
        worktree_path=session.worktree.path,
        session_id=session.session_id,
        progress=session.progress,
        worktree=session.worktree,
    )
    return _finalize_and_mark(
        run_paths=session.run_paths,
        peer_doc=session.peer_doc,
        home_path=session.home.home_dir,
        worktree=session.worktree,
        contract=session.contract,
        original_baseline=session.original_baseline,
        stage=stage,
    )


def run_local_finalize(
    *,
    paths: runstate.RunPaths,
    doc: dict,
) -> dict:
    """Crash-path / socket-failure local finalize after exclusive claim."""
    from groklib.worktree import ExternalWorktree

    home_path = pathlib.Path(str(doc.get("homePath") or ""))
    wt_path = pathlib.Path(str(doc.get("worktreePath") or ""))
    if not wt_path.is_dir():
        raise GrokWrapperError(
            "acp-failure",
            "peer-stop cannot finalize: worktree missing and control socket unavailable",
            {"runId": paths.run_id},
        )
    worktree = ExternalWorktree(
        path=wt_path,
        branch=str(doc.get("worktreeBranch") or ""),
        base_revision=str(doc.get("baseRevision") or ""),
        repo_root=pathlib.Path(str(doc.get("repoRoot") or wt_path)),
    )
    progress = ProgressWriter(paths.run_id, paths.progress_path)
    stage = build_empty_stop_stage(
        run_id=paths.run_id,
        worktree_path=wt_path,
        session_id=doc.get("sessionId") if isinstance(doc.get("sessionId"), str) else None,
        progress=progress,
        worktree=worktree,
    )
    contract = doc.get("contract") if isinstance(doc.get("contract"), dict) else None
    baseline = peer_control.load_original_baseline(doc)
    if not home_path.is_dir():
        home_path = pathlib.Path(tempfile.mkdtemp(prefix=runstate.TEMP_HOME_PREFIX))
        runstate.write_owner_marker(home_path, paths.run_id)
    return _finalize_and_mark(
        run_paths=paths,
        peer_doc=doc,
        home_path=home_path,
        worktree=worktree,
        contract=contract,
        original_baseline=baseline,
        stage=stage,
    )


def run_peer_stop(args: Any, *, load_peer_doc, connect_control, require_experimental_acp) -> dict:
    """peer-stop entry: durable terminal first, then control, then claimed fallback."""
    require_experimental_acp()
    run_id = getattr(args, "run_id", None)
    if not run_id:
        raise GrokWrapperError("usage-error", "peer-stop requires --run-id")
    paths, doc = load_peer_doc(str(run_id))

    # Idempotent path: durable terminal is run_lifecycle SSOT - never re-finalize.
    durable = load_durable_terminal_envelope(paths)
    if durable is not None:
        life = doc.get("lifecycle")
        if life not in _TERMINAL_PEER_LIFECYCLES:
            try:
                mark_peer_lifecycle(
                    paths,
                    "stopped" if durable.get("status") == "success" else "failed",
                )
            except Exception as exc:
                _log("run_peer_stop", "could not align peer lifecycle: {}".format(exc))
        return durable

    socket_path = doc.get("socketPath")
    if isinstance(socket_path, str) and pathlib.Path(socket_path).exists():
        try:
            return connect_control(socket_path, {"op": "stop"}, timeout=1800.0)
        except GrokWrapperError as exc:
            _log(
                "run_peer_stop",
                "socket stop failed, attempting local finalize: {}".format(exc),
            )

    # Exclusive claim before any destructive fallback work.
    outcome, doc, durable2 = claim_peer_stop(paths)
    if outcome == "terminal" and durable2 is not None:
        return durable2
    if outcome == "terminal" and durable2 is None:
        raise GrokWrapperError(
            "acp-failure",
            "peer session already terminal without durable envelope; refusing re-finalize",
            {"runId": run_id},
        )
    if outcome == "wait":
        outcome, doc, durable2 = _return_or_claim_after_wait(paths, run_id=str(run_id))
        if outcome == "terminal" and durable2 is not None:
            return durable2

    # Kill recorded ACP child (start-token match) so a stale grok agent does not
    # keep running in the retained worktree after peer-stop.
    kill_recorded_child(doc)
    # If the resident wrapper is still live after control failure, terminate it
    # only on confirmed identity - never local-finalize while it still serves.
    ensure_wrapper_down_for_fallback(doc)
    return run_local_finalize(paths=paths, doc=doc)
