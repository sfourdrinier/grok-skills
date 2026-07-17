# wrapper/scripts/groklib/modes/peer.py
#
# Experimental ACP peer channel (peer-start / peer-prompt / peer-stop), gated by
# GROK_EXPERIMENTAL_ACP in the WRAPPER (not companion-only). Control plane is a
# wrapper-owned unix socket (0600), not a FIFO. Start parity before first prompt;
# stop finalizes as an honest peer-preview (never integration-ready).

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import platformsupport
from groklib import worktree as worktree_mod
from groklib import worktree_escape
from groklib import acp as acp_mod
from groklib.authhome import (
    PrivateHome,
    create_private_home,
    destroy_private_home,
    render_config_toml,
)

# Patch point for tests (mock.patch.object(peer_mod, "AcpClient", ...)).
AcpClient = acp_mod.AcpClient
from groklib.envelope import (
    arm_peer_resident_stdout_suppress,
    assert_no_secret_material,
    build_envelope,
    clear_peer_resident_stdout_suppress,
    emit_envelope,
    failure_envelope,
    redact_secret_material,
    redact_secret_value_text,
)
from groklib.grokcli import check_version
from groklib.implementation_contract import assert_target_matches, load_contract_file
from groklib.modes import _shared
from groklib.modes import peer_control
from groklib.modes.review import _resolve_target as _resolve_repo_target
from groklib.platformsupport import require_probed_platform_for_live
from groklib.progress import ProgressWriter
from groklib.projectconfig import load_project_config
from groklib.sandbox import policy_for_mode, render_sandbox_toml
from groklib.web_defaults import resolve_web_access

# Re-exports for tests that patch peer_mod._open_control_socket / _assert_peer_uid.
_assert_peer_uid = peer_control.assert_peer_uid
_open_control_socket = peer_control.open_control_socket

# Same tool allowlist as code mode (start parity).
_TOOLS: Tuple[str, ...] = (
    "read_file",
    "grep",
    "list_dir",
    "search_replace",
    "write",
    "run_terminal_command",
)

_SENTINEL_PREFIX = ".grok-run-"
# MAX_PEER_LEASE is separate from MAX_RUN_TIMEOUT; each prompt renews it.
MAX_PEER_LEASE_SECONDS = 2 * 3600


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer", function, message)


_require_experimental_acp = peer_control.require_experimental_acp
_record_peer_worktree = peer_control.record_peer_worktree
_load_original_baseline = peer_control.load_original_baseline


@dataclass
class PeerSession:
    run_id: str
    run_paths: runstate.RunPaths
    session_id: str
    socket_path: pathlib.Path
    acp: Any
    child: Any
    home: PrivateHome
    worktree: worktree_mod.ExternalWorktree
    progress: ProgressWriter
    peer_doc: dict
    contract: Optional[dict]
    original_baseline: Any
    model: str
    sentinel_name: str
    _prompt_lock: threading.Lock = field(default_factory=threading.Lock)
    _prompt_in_flight: bool = False
    _stop_requested: bool = False

    def renew_lease(self) -> None:
        runstate.write_peer_lease(
            self.home.home_dir,
            child_pid=int(self.peer_doc["child"]["pid"]),
            child_start_token=self.peer_doc["child"].get("startToken"),
            lease_seconds=MAX_PEER_LEASE_SECONDS,
        )
        self.peer_doc["leaseExpiresAt"] = time.time() + MAX_PEER_LEASE_SECONDS
        try:
            runstate.write_json_atomic(self.run_paths.run_dir / "peer.json", self.peer_doc)
        except OSError as exc:
            _log("renew_lease", "could not refresh peer.json: {}".format(exc))


def source_grok_dir() -> pathlib.Path:
    return _shared.source_grok_dir()


def _spawn_acp_child(
    *,
    binary: pathlib.Path,
    home: PrivateHome,
    worktree: worktree_mod.ExternalWorktree,
    leader_socket: pathlib.Path,
    model: str,
) -> subprocess.Popen:
    """Spawn ``grok agent stdio`` in the private home / worktree cwd."""
    env = os.environ.copy()
    env["HOME"] = str(home.home_dir)
    env["GROK_HOME"] = str(home.grok_dir)
    # Drop injected secrets from the parent env (defense in depth).
    for key in list(env.keys()):
        lower = key.lower()
        if any(s in lower for s in ("token", "secret", "password", "api_key", "apikey")):
            if lower not in ("inputtokens", "outputtokens"):
                env.pop(key, None)
    # --model belongs on `grok agent` (not the stdio subcommand). Unknown
    # flags after `stdio` make the CLI exit 2 with an empty stdout.
    argv = [str(binary), "agent"]
    if model:
        argv.extend(["--model", model])
    argv.extend(
        [
            "stdio",
            "--leader-socket",
            str(leader_socket),
        ]
    )
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # amendment 4: drop or redact child stderr
            cwd=str(worktree.path),
            env=env,
            **platformsupport.spawn_kwargs_new_group(),
        )
    except OSError as exc:
        raise GrokWrapperError(
            "acp-failure",
            "could not spawn grok agent stdio: {}".format(exc),
            {"binary": str(binary)},
        ) from exc
    return proc


def _assert_start_parity(
    *,
    worktree: worktree_mod.ExternalWorktree,
    home: PrivateHome,
    tools: Tuple[str, ...],
    web_access: bool,
    sentinel_name: str,
    policy: Any,
) -> None:
    """Fail closed before first prompt if code-mode start invariants are unmet."""
    if policy is None or not getattr(policy, "profile", None):
        raise GrokWrapperError(
            "sandbox-failure",
            "start parity: missing sandbox profile capability",
        )
    if not tools:
        raise GrokWrapperError(
            "tool-unavailable",
            "start parity: tool allowlist is empty; refusing unpinned peer session",
        )
    # Plant cwd sentinel (wrapper-owned at start; Grok must keep operating here).
    sentinel_path = worktree.path / sentinel_name
    try:
        sentinel_path.write_text("", encoding="utf-8")
        platformsupport.restrict_file_permissions(sentinel_path)
    except OSError as exc:
        raise GrokWrapperError(
            "wrong-working-directory",
            "start parity: could not plant cwd sentinel: {}".format(exc),
            {"sentinel": sentinel_name},
        ) from exc
    if not sentinel_path.is_file():
        raise GrokWrapperError(
            "wrong-working-directory",
            "start parity: cwd sentinel missing after plant",
            {"sentinel": sentinel_name},
        )
    # No .env in the worktree (code-mode invariant).
    for env_name in (".env", ".env.local"):
        env_path = worktree.path / env_name
        if env_path.exists() or env_path.is_symlink():
            raise GrokWrapperError(
                "validation-failure",
                "start parity: {} must not be present in the peer worktree".format(env_name),
                {"path": str(env_path)},
            )
    # Private home must exist and be 0700 on POSIX.
    if not home.home_dir.is_dir():
        raise GrokWrapperError("sandbox-failure", "start parity: private home missing")
    if platformsupport.is_posix():
        mode = stat.S_IMODE(home.home_dir.stat().st_mode)
        if mode != 0o700:
            raise GrokWrapperError(
                "sandbox-failure",
                "start parity: private home mode is {:o}, expected 0700".format(mode),
            )


def _extract_chunk_text(notification: dict) -> str:
    params = notification.get("params") or {}
    update = params.get("update") or {}
    content = update.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(update.get("text"), str):
        return update["text"]
    return ""


def _handle_prompt(session: PeerSession, task: str) -> dict:
    """Run one serialized session/prompt; relay redacted chunks; return turn envelope."""
    if session._prompt_in_flight or not session._prompt_lock.acquire(blocking=False):
        raise GrokWrapperError(
            "acp-failure",
            "a prompt is already in flight for this peer session",
            {"runId": session.run_id},
        )
    session._prompt_in_flight = True
    try:
        # Child liveness
        child = session.child
        if child is not None and getattr(child, "poll", lambda: None)() is not None:
            session.peer_doc["lifecycle"] = "died"
            try:
                runstate.write_json_atomic(session.run_paths.run_dir / "peer.json", session.peer_doc)
            except OSError:
                pass
            raise GrokWrapperError(
                "acp-failure",
                "peer child is dead; reattach is not supported in v1 - run peer-stop then peer-start",
                {"runId": session.run_id, "hint": "reattach-unsupported"},
            )

        texts: List[str] = []

        def _on_update(note: dict) -> None:
            text = _extract_chunk_text(note)
            if not text:
                return
            texts.append(text)
            # Redact before progress.jsonl (amendment 4).
            redacted = redact_secret_value_text(text)
            session.progress.safe_emit(
                "grok",
                "session/update",
                data={"text": redacted, "sessionUpdate": True},
            )

        result = session.acp.session_prompt(
            session_id=session.session_id,
            text=task,
            on_update=_on_update,
        )
        session.renew_lease()
        combined = "".join(texts)
        redacted_result = redact_secret_material(
            {
                "text": combined,
                "stopReason": result.get("stopReason"),
                "usage": result.get("usage"),
            },
            redact_keys=True,
        )
        turn_env = build_envelope(
            run_id=session.run_id,
            mode="peer-prompt",
            status="success",
            requestedModel=session.model,
            effectiveModel=session.model,
            progressStreamPath=str(session.run_paths.progress_path),
            response={"peer": {"sessionId": session.session_id}, "result": redacted_result},
            grok={
                "sessionId": session.session_id,
                "requestId": None,
                "stopReason": result.get("stopReason"),
                "modelUsage": None,
            },
        )
        # Defense in depth: same scan as emit_envelope before control-plane leave.
        assert_no_secret_material(turn_env)
        return turn_env
    finally:
        session._prompt_in_flight = False
        try:
            session._prompt_lock.release()
        except RuntimeError:
            pass


def _handle_control_connection(session: PeerSession, conn: Any) -> Optional[dict]:
    """Serve one control-plane request. Returns final envelope when op=stop."""
    uid = peer_control.peer_cred_uid(conn)
    if uid is not None:
        _assert_peer_uid(uid)
    req = peer_control.read_json_line(conn)
    op = req.get("op")
    if op == "prompt":
        task = req.get("task")
        if not isinstance(task, str) or not task.strip():
            peer_control.write_json_line(
                conn,
                {
                    "type": "error",
                    "error": {"class": "usage-error", "message": "prompt requires task text"},
                },
            )
            return None
        try:
            env = _handle_prompt(session, task)
            peer_control.write_json_line(conn, {"type": "result", "envelope": env})
        except GrokWrapperError as exc:
            err_env = failure_envelope(
                run_id=session.run_id,
                mode="peer-prompt",
                error_class=exc.error_class,
                message=str(exc),
                detail=exc.detail or None,
            )
            peer_control.write_json_line(conn, {"type": "result", "envelope": err_env})
        return None
    if op == "stop":
        session._stop_requested = True
        final = _stop_session(session)
        peer_control.write_json_line(conn, {"type": "result", "envelope": final})
        return final
    peer_control.write_json_line(
        conn,
        {"type": "error", "error": {"class": "usage-error", "message": "unknown op {!r}".format(op)}},
    )
    return None


def _stop_session(session: PeerSession) -> dict:
    """session/cancel, tear down child, code finalize path, destroy home."""
    from groklib.modes import peer_finalize

    try:
        session.acp.session_cancel(session_id=session.session_id)
    except Exception as exc:
        _log("_stop_session", "cancel: {}".format(exc))
    try:
        session.acp.close()
    except Exception as exc:
        _log("_stop_session", "acp close: {}".format(exc))
    child = session.child
    if child is not None:
        try:
            platformsupport.kill_process_tree(child)
        except Exception as exc:
            _log("_stop_session", "kill child: {}".format(exc))
        try:
            child.wait(timeout=5)
        except Exception:
            pass

    # Build a finalize stage compatible with code_handoff_finalize.
    class _Acc:
        def __init__(self) -> None:
            self.commands: List[dict] = []
            self.changed_files: List[str] = []
            self.diff_summary: Optional[str] = None
            self.effective_working_directory = str(session.worktree.path)
            self.warnings: List[str] = []
            self.verifier = None

    class _Stage:
        pass

    stage = _Stage()
    stage.worktree = session.worktree
    stage.run_id = session.run_id
    stage.acc = _Acc()
    stage.progress = session.progress
    stage.result = type(
        "R",
        (),
        {
            "answer": "",
            "session_id": session.session_id,
            "request_id": None,
            "stop_reason": "cancelled",
            "model_usage": None,
            "turns": None,
            "raw_usage": None,
            "parsed": {},
            "stderr": "",
        },
    )()

    return peer_finalize.finalize_peer_session(
        run_paths=session.run_paths,
        peer_doc=session.peer_doc,
        home_path=session.home.home_dir,
        worktree=session.worktree,
        contract=session.contract,
        original_baseline=session.original_baseline,
        stage=stage,
    )


def _serve_control_plane(
    session: PeerSession, running_env: dict, preopened: Any = None
) -> dict:
    """Block serving the control socket until stop; return the final envelope."""
    del running_env  # reserved for future progress on the control plane

    def _open(_path):
        if preopened is not None:
            return preopened
        return _open_control_socket(session.socket_path)

    return peer_control.serve_until_stop(
        session,
        open_socket=_open,
        handle_connection=_handle_control_connection,
        stop_session=_stop_session,
    )


def _load_peer_doc(run_id: str) -> Tuple[runstate.RunPaths, dict]:
    if not runstate.is_valid_run_id(run_id):
        raise GrokWrapperError("invalid-target", "not a valid run id: {!r}".format(run_id))
    paths = runstate.RunPaths(
        run_id=run_id,
        run_dir=runstate.state_root() / "runs" / run_id,
        progress_path=runstate.state_root() / "runs" / run_id / "progress.jsonl",
        envelope_path=runstate.state_root() / "runs" / run_id / "envelope.json",
        trace_dir=runstate.state_root() / "runs" / run_id / "trace",
    )
    peer_path = paths.run_dir / "peer.json"
    if not peer_path.is_file():
        raise GrokWrapperError(
            "invalid-target",
            "no peer session for run {}".format(run_id),
            {"runId": run_id},
        )
    try:
        doc = json.loads(peer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GrokWrapperError(
            "acp-failure",
            "peer.json unreadable: {}".format(exc),
            {"runId": run_id},
        ) from exc
    if not isinstance(doc, dict):
        raise GrokWrapperError("acp-failure", "peer.json is not an object")
    return paths, doc


def _connect_control(socket_path: str, payload: dict, timeout: float = 900.0) -> dict:
    return peer_control.connect_control(socket_path, payload, timeout=timeout)


def run_peer_start(args: argparse.Namespace) -> dict:
    """Create run + worktree + home + ACP child; emit running; serve control socket."""
    _require_experimental_acp()
    clear_peer_resident_stdout_suppress()
    require_probed_platform_for_live()
    binary = _shared.resolve_binary(args)
    check_version(binary)
    runstate.best_effort_reap_stale_temp_homes(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS)

    target = getattr(args, "target", None)
    base = getattr(args, "base", None)
    if not target or not base:
        raise GrokWrapperError(
            "usage-error",
            "peer-start requires --target and --base",
            {"target": target, "base": base},
        )
    repo_root, target_abs, target_relative = _resolve_repo_target(target)
    model = getattr(args, "model", None) or "grok-4.5"
    web_access = resolve_web_access("peer", getattr(args, "web", None))
    timeout = int(getattr(args, "timeout", None) or 900)

    contract = None
    contract_file = getattr(args, "contract_file", None)
    if contract_file is not None and str(contract_file).strip():
        contract = load_contract_file(pathlib.Path(str(contract_file).strip()))
        cli_target = target_relative if target_relative else "."
        assert_target_matches(contract, cli_target)

    original_baseline = worktree_escape.capture_original_checkout_baseline(repo_root)
    project_config = load_project_config(repo_root)

    run_paths = runstate.create_run("peer-start")
    progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
    progress.safe_emit("start", "peer-start run created")

    required_paths: Tuple[str, ...] = (target_relative,) if target_relative else ()
    worktree_mod.assert_committed_base_sufficient(repo_root, base, required_paths)
    worktree = worktree_mod.create_external_worktree(
        repo_root=repo_root, base=base, run_id=run_paths.run_id
    )
    worktree_mod.verify_external_worktree(worktree)
    progress.safe_emit("worktree", "worktree ready", data={"worktree": str(worktree.path)})
    # Record worktree ownership on run.json immediately (cleanup rebuild path).
    _record_peer_worktree(
        run_paths,
        model=model,
        repo_root=repo_root,
        target_relative=target_relative,
        worktree=worktree,
    )

    # Capture pristine gate scripts (start parity) before any model edit.
    from groklib.modes.code import _read_committed_manifest_fields, _target_in_worktree

    _pristine_name, pristine_scripts = _read_committed_manifest_fields(
        _target_in_worktree(worktree.path, target_relative)
    )
    del _pristine_name  # captured for parity; gate runs at stop via finalize

    private_tmp = pathlib.Path(tempfile.mkdtemp(prefix="gs-peer-tmp-"))
    try:
        platformsupport.restrict_dir_permissions(private_tmp)
    except OSError:
        pass
    policy = policy_for_mode("peer", worktree=worktree.path, private_tmp=private_tmp)
    real_home = source_grok_dir().parent
    sandbox_toml = render_sandbox_toml(policy, real_home=real_home)
    config_toml = render_config_toml(mode="peer")
    home = create_private_home(
        source_grok_dir=source_grok_dir(),
        auth_file_names=_shared.AUTH_FILE_NAMES,
        config_toml=config_toml,
        sandbox_toml=sandbox_toml,
    )
    leader_socket = runstate.allocate_leader_socket(home.home_dir, run_paths.run_id)
    sentinel_name = _SENTINEL_PREFIX + run_paths.run_id

    # START PARITY fail closed BEFORE spawn (amendment 3).
    _assert_start_parity(
        worktree=worktree,
        home=home,
        tools=_TOOLS,
        web_access=web_access,
        sentinel_name=sentinel_name,
        policy=policy,
    )
    progress.safe_emit("sandbox", "start parity ok", data={"profile": policy.profile})

    child = _spawn_acp_child(
        binary=binary,
        home=home,
        worktree=worktree,
        leader_socket=leader_socket,
        model=model,
    )
    acp = AcpClient(child, timeout_seconds=timeout)
    try:
        init = acp.initialize()
        # Register pre_tool_use deny hook (amendment 6: NON-enforcement; OS sandbox enforces).
        hooks = ((init.get("_meta") or {}).get("x.ai/hooks") or {})
        if "pre_tool_use" in (hooks.get("blockingEvents") or []):
            progress.safe_emit(
                "sandbox",
                "pre_tool_use deny hook registered (documented NON-enforcement; "
                "OS sandbox is the enforcement layer; terminal-command policy: "
                "allowlisted only inside worktree write confinement)",
                data={"enforcement": "non-enforcement-advisory"},
            )
        session = acp.session_new(cwd=str(worktree.path), mcp_servers=[])
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise GrokWrapperError("acp-failure", "session/new returned no sessionId")
    except Exception:
        try:
            acp.close()
        except Exception:
            pass
        try:
            destroy_private_home(home)
        except Exception:
            pass
        raise

    # Control socket under the private home (short path) - run-dir paths under a
    # nested XDG_STATE_HOME often exceed the AF_UNIX ~104-byte limit.
    socket_path = home.home_dir / ".grok" / "p-{}.sock".format(
        run_paths.run_id.rsplit("-", 1)[-1]
    )
    encoded_len = len(str(socket_path).encode("utf-8"))
    if encoded_len >= 100:
        raise GrokWrapperError(
            "acp-failure",
            "peer control socket path exceeds 100-byte AF_UNIX guard",
            {"path": str(socket_path), "bytes": encoded_len},
        )
    wrapper_pid = os.getpid()
    child_pid = int(child.pid)
    peer_doc = {
        "schemaVersion": 1,
        "lifecycle": "running",
        "sessionId": session_id,
        "socketPath": str(socket_path),
        "wrapper": {
            "pid": wrapper_pid,
            "startToken": platformsupport.process_start_token(wrapper_pid),
        },
        "child": {
            "pid": child_pid,
            "startToken": platformsupport.process_start_token(child_pid),
        },
        "homePath": str(home.home_dir),
        "worktreePath": str(worktree.path),
        "worktreeBranch": worktree.branch,
        "baseRevision": worktree.base_revision,
        "repoRoot": str(repo_root),
        "targetRelative": target_relative,
        "sentinelName": sentinel_name,
        "contract": contract,
        # Persist start baseline for crash-path peer-stop (never re-capture at stop).
        "originalBaseline": dict(original_baseline) if original_baseline is not None else {},
        "leaseExpiresAt": time.time() + MAX_PEER_LEASE_SECONDS,
        "model": model,
        "webAccess": web_access,
        "pristineScriptsCaptured": pristine_scripts is not None,
        "projectPackageManager": project_config.package_manager,
    }
    runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)
    runstate.write_peer_lease(
        home.home_dir,
        child_pid=child_pid,
        child_start_token=peer_doc["child"].get("startToken"),
        lease_seconds=MAX_PEER_LEASE_SECONDS,
    )

    # Persist baseline snapshot as JSON-friendly dict for stop (paths only).
    # original_baseline stays in-process for the resident wrapper.
    if contract is not None:
        from groklib.modes.code_continue import write_contract_json

        write_contract_json(run_paths.run_dir, contract)

    running_env = build_envelope(
        run_id=run_paths.run_id,
        mode="peer-start",
        status="running",
        requestedModel=model,
        effectiveModel=model,
        repository=str(repo_root),
        targetWorkspace=target_relative or ".",
        effectiveWorkingDirectory=str(worktree.path),
        worktreePath=str(worktree.path),
        worktreeBranch=worktree.branch,
        baseRevision=worktree.base_revision,
        progressStreamPath=str(run_paths.progress_path),
        policy={
            "tools": list(_TOOLS),
            "permissionMode": "auto",
            "subagents": False,
            "webAccess": web_access,
            "memory": False,
        },
        response={
            "peer": {
                "sessionId": session_id,
                "socketPath": str(socket_path),
            }
        },
        cleanup={"status": "retained", "detail": str(worktree.path)},
    )

    session_obj = PeerSession(
        run_id=run_paths.run_id,
        run_paths=run_paths,
        session_id=session_id,
        socket_path=socket_path,
        acp=acp,
        child=child,
        home=home,
        worktree=worktree,
        progress=progress,
        peer_doc=peer_doc,
        contract=contract,
        original_baseline=original_baseline,
        model=model,
        sentinel_name=sentinel_name,
    )

    # Open the control socket BEFORE emitting the running envelope so a
    # companion that immediately peer-prompts cannot race a missing socket.
    control_srv = _open_control_socket(session_obj.socket_path)
    emit_envelope(running_env, None)
    try:
        import sys

        sys.stdout.flush()
    except Exception:
        pass

    final = _serve_control_plane(session_obj, running_env, preopened=control_srv)
    # Resident process: exactly ONE stdout envelope (the running one above).
    # Terminal outcome is delivered on the control socket to peer-stop and
    # durable in the run dir; suppress the entrypoint's post-return emit.
    final = dict(final)
    final["_peerStartAlreadyEmittedRunning"] = True
    arm_peer_resident_stdout_suppress()
    return final


def run_peer_prompt(args: argparse.Namespace) -> dict:
    _require_experimental_acp()
    run_id = getattr(args, "run_id", None)
    if not run_id:
        raise GrokWrapperError("usage-error", "peer-prompt requires --run-id")
    task = _shared.resolve_task_text(args)
    paths, doc = _load_peer_doc(str(run_id))
    lifecycle = doc.get("lifecycle")
    if lifecycle in ("died", "stopped"):
        raise GrokWrapperError(
            "acp-failure",
            "peer session lifecycle is {!r}; reattach unsupported - peer-stop then peer-start".format(
                lifecycle
            ),
            {"runId": run_id, "lifecycle": lifecycle, "hint": "reattach-unsupported"},
        )
    # Child identity check via peer.json
    child = doc.get("child") or {}
    child_pid = child.get("pid")
    child_token = child.get("startToken")
    if isinstance(child_pid, int):
        if not platformsupport.process_is_alive(child_pid):
            doc["lifecycle"] = "died"
            try:
                runstate.write_json_atomic(paths.run_dir / "peer.json", doc)
            except OSError:
                pass
            raise GrokWrapperError(
                "acp-failure",
                "peer child is dead; reattach unsupported - run peer-stop then peer-start",
                {"runId": run_id, "hint": "reattach-unsupported"},
            )
        if isinstance(child_token, str):
            current = platformsupport.process_start_token(child_pid)
            if current is not None and current != child_token:
                doc["lifecycle"] = "died"
                try:
                    runstate.write_json_atomic(paths.run_dir / "peer.json", doc)
                except OSError:
                    pass
                raise GrokWrapperError(
                    "acp-failure",
                    "peer child pid recycled; reattach unsupported",
                    {"runId": run_id, "hint": "reattach-unsupported"},
                )
    socket_path = doc.get("socketPath")
    if not isinstance(socket_path, str) or not socket_path:
        raise GrokWrapperError("acp-failure", "peer.json missing socketPath")
    return _connect_control(socket_path, {"op": "prompt", "task": task})


def run_peer_stop(args: argparse.Namespace) -> dict:
    _require_experimental_acp()
    run_id = getattr(args, "run_id", None)
    if not run_id:
        raise GrokWrapperError("usage-error", "peer-stop requires --run-id")
    paths, doc = _load_peer_doc(str(run_id))
    socket_path = doc.get("socketPath")
    if isinstance(socket_path, str) and pathlib.Path(socket_path).exists():
        try:
            return _connect_control(socket_path, {"op": "stop"}, timeout=1800.0)
        except GrokWrapperError as exc:
            _log("run_peer_stop", "socket stop failed, attempting local finalize: {}".format(exc))
    # Fallback: local finalize when resident wrapper is already gone.
    # MUST use original_baseline from peer-start (never re-capture: closes escape window).
    from groklib.modes import peer_finalize
    from groklib.worktree import ExternalWorktree

    home_path = pathlib.Path(str(doc.get("homePath") or ""))
    wt_path = pathlib.Path(str(doc.get("worktreePath") or ""))
    if not wt_path.is_dir():
        raise GrokWrapperError(
            "acp-failure",
            "peer-stop cannot finalize: worktree missing and control socket unavailable",
            {"runId": run_id},
        )
    worktree = ExternalWorktree(
        path=wt_path,
        branch=str(doc.get("worktreeBranch") or ""),
        base_revision=str(doc.get("baseRevision") or ""),
        repo_root=pathlib.Path(str(doc.get("repoRoot") or wt_path)),
    )

    class _Acc:
        commands: List[dict] = []
        changed_files: List[str] = []
        diff_summary = None
        effective_working_directory = str(wt_path)
        warnings: List[str] = []
        verifier = None

    class _Stage:
        pass

    stage = _Stage()
    stage.worktree = worktree
    stage.run_id = paths.run_id
    stage.acc = _Acc()
    stage.progress = ProgressWriter(paths.run_id, paths.progress_path)
    stage.result = type(
        "R",
        (),
        {
            "answer": "",
            "session_id": doc.get("sessionId"),
            "request_id": None,
            "stop_reason": "cancelled",
            "model_usage": None,
            "turns": None,
            "raw_usage": None,
            "parsed": {},
            "stderr": "",
        },
    )()
    contract = doc.get("contract") if isinstance(doc.get("contract"), dict) else None
    baseline = _load_original_baseline(doc)
    if not home_path.is_dir():
        # Home already gone; still label + finalize artifacts.
        home_path = pathlib.Path(tempfile.mkdtemp(prefix=runstate.TEMP_HOME_PREFIX))
        runstate.write_owner_marker(home_path, paths.run_id)
    return peer_finalize.finalize_peer_session(
        run_paths=paths,
        peer_doc=doc,
        home_path=home_path,
        worktree=worktree,
        contract=contract,
        original_baseline=baseline,
        stage=stage,
    )


def run(args: argparse.Namespace) -> dict:
    """Dispatch peer-start / peer-prompt / peer-stop from a single module entry."""
    mode = getattr(args, "mode", None) or getattr(args, "peer_mode", None)
    if mode == "peer-start":
        return run_peer_start(args)
    if mode == "peer-prompt":
        return run_peer_prompt(args)
    if mode == "peer-stop":
        return run_peer_stop(args)
    raise GrokWrapperError("usage-error", "unknown peer mode: {!r}".format(mode))
