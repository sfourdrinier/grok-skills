# wrapper/scripts/groklib/modes/peer_process.py
#
# ACP child-process lifecycle helpers for the peer channel: spawning the
# `grok agent stdio` child under the minimal env + workspace sandbox profile, and
# a fail-safe guarded kill of a peer.json-recorded child. Extracted from peer.py
# to keep it under the 900-line cap; re-imported there under the same names so
# the existing peer_mod._spawn_acp_child / _kill_recorded_child surface (and the
# tests that patch it) keep resolving. Start-only argv assembly lives here so
# peer.py stays under the line cap while the child pins the same C6 globals
# (permission/tools/subagents/memory/web) the envelope advertises.

import os
import pathlib
import stat
import subprocess
from typing import Any, List, Sequence, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import grokcli
from groklib import platformsupport
from groklib import worktree as worktree_mod
from groklib.authhome import PrivateHome, destroy_private_home


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer_process", function, message)


def build_acp_stdio_argv(
    *,
    binary: pathlib.Path,
    model: str,
    leader_socket: pathlib.Path,
    policy: Any,
    tools: Sequence[str],
    web_access: bool,
) -> List[str]:
    """Build ``grok <C6 globals> agent [--model] stdio --leader-socket`` argv.

    Live probe (grok 0.2.104): global flags before ``agent`` are accepted for
    ``agent stdio`` (``--permission-mode``, ``--tools``, ``--no-subagents``,
    ``--no-memory``, ``--disable-web-search``, ``--sandbox``). The same flags
    after ``stdio`` or on ``grok agent`` (without being global) are rejected.
    Tool expansion uses ``grokcli.effective_tools`` (D-WEB single source) so the
    child allowlist matches envelope ``policy.tools`` from ``_policy_field``.
    """
    argv: List[str] = [str(binary)]
    # Global C6 pins BEFORE the agent subcommand (not after stdio).
    if policy is not None and getattr(policy, "profile", None):
        argv.extend(["--sandbox", policy.profile])
    argv.extend(["--permission-mode", grokcli.HEADLESS_PERMISSION_MODE])
    effective = grokcli.effective_tools(tuple(tools), web_access)
    if effective:
        argv.extend(["--tools", ",".join(effective)])
    else:
        # Fail closed: empty allowlist denies every built-in (same as build_argv).
        argv.extend(["--disallowed-tools", ",".join(grokcli.ALL_BUILTIN_TOOLS)])
    argv.append("--no-subagents")
    argv.append("--no-memory")
    if not web_access:
        argv.append("--disable-web-search")
    argv.append("agent")
    if model:
        argv.extend(["--model", model])
    argv.extend(["stdio", "--leader-socket", str(leader_socket)])
    return argv


def register_active_child(proc: subprocess.Popen) -> None:
    """Register ACP child on the SIGTERM active-process SSOT (grokcli)."""
    grokcli._register_active_proc(proc)


def unregister_active_child(proc_or_pid: Any = None) -> None:
    """Drop ACP child from the SIGTERM active-process SSOT (best-effort).

    Accepts a ``Popen`` handle or an int pid. Pid form scans the shared registry
    (needed when peer-stop only has peer.json identity, not the resident handle).
    """
    try:
        if proc_or_pid is None:
            return
        if isinstance(proc_or_pid, int) and not isinstance(proc_or_pid, bool):
            with grokcli._ACTIVE_PROCS_LOCK:
                victims = [
                    p
                    for p in list(grokcli._ACTIVE_PROCS)
                    if getattr(p, "pid", None) == proc_or_pid
                ]
            for proc in victims:
                grokcli._unregister_active_proc(proc)
            return
        grokcli._unregister_active_proc(proc_or_pid)
    except Exception as exc:  # pragma: no cover - defensive
        _log("unregister_active_child", "unregister failed: {}".format(exc))


def require_process_identity_token(pid: int, *, role: str) -> str:
    """Return a non-empty process startToken or fail closed.

    Peer kill / lease / stopOwner identity depends on startToken; null or empty
    would fail-open toward pid-only matching (unsafe under pid reuse).
    """
    token = platformsupport.process_start_token(pid)
    if not isinstance(token, str) or not token:
        raise GrokWrapperError(
            "acp-failure",
            "missing {} startToken for pid {}; refusing peer identity record".format(
                role, pid
            ),
            {"role": role, "pid": pid, "startToken": token},
        )
    return token


def spawn_acp_child(
    *,
    binary: pathlib.Path,
    home: PrivateHome,
    worktree: worktree_mod.ExternalWorktree,
    leader_socket: pathlib.Path,
    model: str,
    policy: Any,
    tools: Sequence[str],
    web_access: bool = False,
) -> subprocess.Popen:
    """Spawn ``grok agent stdio`` in the private home / worktree cwd.

    Uses the SAME minimal child env (HOME/PATH/TMPDIR only, GROK_SANDBOX unset)
    and the SAME global ``--sandbox <profile>`` + C6 tool/permission/web pins as
    code mode (grokcli.build_argv / _minimal_env): copying os.environ leaked
    operator credentials into the long-lived model process, and omitting those
    globals let the child run under CLI defaults while the envelope advertised
    confinement. Flags are global before ``agent`` (probe-accepted placement).

    Spawn + active-proc registration is SIGTERM-blocked (same SSOT as code-mode
    grokcli.spawn) so a harness SIGTERM cannot orphan the credential-bearing
    long-lived ACP child.
    """
    env = grokcli._minimal_env(home, binary)
    argv = build_acp_stdio_argv(
        binary=binary,
        model=model,
        leader_socket=leader_socket,
        policy=policy,
        tools=tools,
        web_access=web_access,
    )
    try:
        with grokcli._sigterm_blocked():
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
            register_active_child(proc)
    except GrokWrapperError:
        raise
    return proc


def assert_start_parity(
    *,
    worktree: worktree_mod.ExternalWorktree,
    home: PrivateHome,
    tools: Tuple[str, ...],
    web_access: bool,
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
    # The cwd sentinel is NOT planted by the wrapper: a wrapper-planted sentinel
    # makes the stop-time proof vacuous (it passes even if Grok never operated in
    # the worktree). Instead Grok is instructed to create it as its mandatory
    # first action on the first peer-prompt (see _handle_prompt), matching code
    # mode, so the stop-time check is genuine evidence Grok ran in the worktree.
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


def kill_recorded_child(doc: dict, *, proc: Any = None) -> None:
    """Kill the peer.json-recorded ACP child ONLY on positively-confirmed identity.

    Fail safe: a kill sends SIGKILL to a whole process group, so unless the
    recorded start-token is present AND still matches the live pid's token, do
    nothing - never kill a pid we cannot prove is our child (it may be recycled
    to an unrelated process). Best-effort; never raises. When ``proc`` is the
    live Popen (or after a confirmed pid kill), unregister from the SIGTERM
    active-process registry so terminate_active_processes does not retain a
    stale handle.
    """
    child = doc.get("child") or {}
    child_pid = child.get("pid")
    child_token = child.get("startToken")
    if not isinstance(child_pid, int) or not isinstance(child_token, str) or not child_token:
        if proc is not None:
            unregister_active_child(proc)
        return
    try:
        if not platformsupport.process_is_alive(child_pid):
            if proc is not None:
                unregister_active_child(proc)
            return
        current = platformsupport.process_start_token(child_pid)
        if current is None or current != child_token:
            return  # cannot positively confirm identity; do not kill
        # Never killpg a process in OUR OWN group: the real ACP child is spawned
        # in a NEW group (spawn_kwargs_new_group), so a same-group pid is either a
        # mis-detached child or a test-recorded pid, and killing its group would
        # take down the wrapper itself.
        if platformsupport.is_posix():
            try:
                if os.getpgid(child_pid) == os.getpgid(0):
                    return
            except OSError:
                return
        platformsupport.kill_process_tree_by_pid(child_pid)
        unregister_active_child(proc if proc is not None else child_pid)
    except Exception as exc:
        _log("kill_recorded_child", "could not kill child {}: {}".format(child_pid, exc))


class StartResources:
    """Tracks resources created during peer-start so an abort can tear them down."""

    def __init__(self) -> None:
        self.worktree = None
        self.home = None
        self.child = None
        self.acp = None


def abort_peer_start(*, run_paths, progress, res, error) -> None:
    """Tear down a peer-start that failed BEFORE it began serving.

    Closes the ACP client, kills the child, destroys the private home, removes
    the external worktree + branch, and terminalizes the run under its OWN id
    (mirrors the shared worktree lifecycle) so a start-time failure leaves no
    orphaned credential home / worktree / grok-agent process. Best-effort; each
    step is independent and never raises.
    """
    if res.acp is not None:
        try:
            res.acp.close()
        except Exception as exc:
            _log("abort_peer_start", "acp close failed: {}".format(exc))
    if res.child is not None:
        try:
            platformsupport.kill_process_tree(res.child)
        except Exception as exc:
            _log("abort_peer_start", "kill child failed: {}".format(exc))
        try:
            unregister_active_child(res.child)
        except Exception as exc:
            _log("abort_peer_start", "unregister child failed: {}".format(exc))
    if res.home is not None:
        try:
            destroy_private_home(res.home)
        except Exception as exc:
            _log("abort_peer_start", "destroy home failed: {}".format(exc))
    if res.worktree is not None:
        try:
            worktree_mod.remove_external_worktree(
                res.worktree, confirmed=True, expected_run_id=run_paths.run_id
            )
        except Exception as exc:
            _log("abort_peer_start", "remove worktree failed: {}".format(exc))
    try:
        from groklib.envelope import failure_envelope
        from groklib.modes import peer_finalize

        env = failure_envelope(
            run_id=run_paths.run_id,
            mode="peer-start",
            error_class=getattr(error, "error_class", None) or "acp-failure",
            message=str(error) if error is not None else "peer-start aborted before serving",
            detail=getattr(error, "detail", None) or None,
            progressStreamPath=str(run_paths.progress_path),
        )
        peer_finalize._terminalize_peer_run(run_paths, env)
    except Exception as exc:
        _log("abort_peer_start", "terminalize failed: {}".format(exc))
    try:
        progress.safe_emit("cleanup", "peer-start aborted; resources torn down")
    except Exception:
        pass
