# wrapper/scripts/groklib/modes/peer_control.py
#
# Wrapper-owned unix control socket for the ACP peer channel (0600, companion-uid
# only) plus small peer lifecycle helpers shared with peer.py (900-line cap).
# ACP is default; GROK_DISABLE_ACP=1 is the opt-out.

from __future__ import annotations

import json
import os
import pathlib
import socket
import struct
from typing import Any, Dict, Optional

from groklib import GrokWrapperError, log_stderr, platformsupport, runstate
from groklib.envelope import assert_no_secret_material, redact_secret_material

_SOCKET_BACKLOG = 4
_CONTROL_ACCEPT_TIMEOUT = 1.0


def _log(function: str, message: str) -> None:
    log_stderr("modes.peer_control", function, message)


def require_experimental_acp() -> None:
    """Fail closed only when ACP is explicitly disabled (opt-out, Task 7.4).

    Peer modes work by default. Set GROK_DISABLE_ACP=1 to force one-shot code.
    GROK_EXPERIMENTAL_ACP is no longer a hard gate (legacy opt-in is ignored).
    """
    flag = os.environ.get("GROK_DISABLE_ACP", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        raise GrokWrapperError(
            "usage-error",
            "peer channel disabled via GROK_DISABLE_ACP=1; unset it to use ACP peer modes",
        )


def record_peer_worktree(
    run_paths: runstate.RunPaths,
    *,
    model: str,
    repo_root: pathlib.Path,
    target_relative: str,
    worktree: Any,
) -> None:
    """Advance lifecycle + record worktree fields so cleanup can rebuild/remove."""
    try:
        record = runstate.load_run_record(run_paths.run_id)
        rev = int(record.get("recordRevision", 0))
        if record.get("lifecycle") == "created":
            record = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(record["recordRevision"])
        runstate.cas_update_run_record(
            run_paths,
            rev,
            {
                "requestedModel": model,
                "repository": str(repo_root),
                "targetWorkspace": target_relative or ".",
                "worktreePath": str(worktree.path),
                "worktreeBranch": worktree.branch,
                "baseRevision": worktree.base_revision,
                "status": "running",
            },
        )
    except GrokWrapperError:
        raise
    except Exception as exc:
        raise GrokWrapperError(
            "state-ownership-violation",
            "could not record peer worktree ownership on run.json: {}".format(exc),
            {"runId": run_paths.run_id, "reason": "worktree-metadata-cas-failed"},
        ) from exc


def load_original_baseline(peer_doc: dict) -> Dict[str, str]:
    """Load the start-captured baseline; never re-capture at stop (escape window)."""
    raw = peer_doc.get("originalBaseline")
    if not isinstance(raw, dict):
        raise GrokWrapperError(
            "acp-failure",
            "peer-stop missing originalBaseline from peer-start; refusing re-capture",
            {"hint": "start-baseline-required"},
        )
    # Empty dict is valid when the checkout was clean at start.
    out: Dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def assert_peer_uid(peer_uid: int) -> None:
    """Refuse control-plane clients that are not the companion uid (wrapper uid)."""
    if not platformsupport.is_posix():
        return
    if peer_uid != os.getuid():
        raise GrokWrapperError(
            "acp-failure",
            "control socket refused: foreign uid {}".format(peer_uid),
            {"peerUid": peer_uid, "ownerUid": os.getuid()},
        )


def open_control_socket(path: pathlib.Path) -> socket.socket:
    """Create a wrapper-owned AF_UNIX stream socket at ``path`` with mode 0600."""
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            raise GrokWrapperError(
                "acp-failure",
                "could not remove stale control socket: {}".format(exc),
                {"path": str(path)},
            ) from exc
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
        os.chmod(str(path), 0o600)
        srv.listen(_SOCKET_BACKLOG)
        srv.settimeout(_CONTROL_ACCEPT_TIMEOUT)
    except OSError as exc:
        srv.close()
        raise GrokWrapperError(
            "acp-failure",
            "could not open control socket: {}".format(exc),
            {"path": str(path)},
        ) from exc
    return srv


def peer_cred_uid(conn: socket.socket) -> Optional[int]:
    """Best-effort peer credential uid (Linux SO_PEERCRED / macOS LOCAL_PEERCRED)."""
    if not platformsupport.is_posix():
        return None
    try:
        SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
        creds = conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return int(uid)
    except (OSError, struct.error, ValueError):
        pass
    try:
        SOL_LOCAL = getattr(socket, "SOL_LOCAL", 0)
        LOCAL_PEERCRED = 1
        raw = conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, 128)
        if len(raw) >= 8:
            _ver, uid = struct.unpack_from("II", raw, 0)
            return int(uid)
    except (OSError, struct.error, ValueError):
        pass
    return None


def read_json_line(conn: socket.socket, timeout: float = 30.0) -> dict:
    conn.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = conn.recv(4096)
        except socket.timeout as exc:
            raise GrokWrapperError("acp-failure", "control socket read timed out") from exc
        if not chunk:
            raise GrokWrapperError("acp-failure", "control socket closed by peer")
        buf += chunk
        if len(buf) > 4 * 1024 * 1024:
            raise GrokWrapperError("acp-failure", "control socket frame too large")
    line, _rest = buf.split(b"\n", 1)
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GrokWrapperError("acp-failure", "malformed control frame: {}".format(exc)) from exc
    if not isinstance(obj, dict):
        raise GrokWrapperError("acp-failure", "control frame must be a JSON object")
    return obj


def write_json_line(conn: socket.socket, obj: dict) -> None:
    """Send one control-plane JSON line after redaction + secret scan (emit_envelope parity)."""
    safe = redact_secret_material(obj, redact_keys=True)
    # Same fail-closed guarantee as emit_envelope: residual secret shapes never leave.
    assert_no_secret_material(safe)
    data = (json.dumps(safe, separators=(",", ":")) + "\n").encode("utf-8")
    conn.sendall(data)


def connect_control(socket_path: str, payload: dict, timeout: float = 900.0) -> dict:
    """Client side: connect to the resident control socket and exchange one request."""
    path = pathlib.Path(socket_path)
    if not path.exists():
        raise GrokWrapperError(
            "acp-failure",
            "peer control socket missing; child may have died - reattach unsupported, run peer-stop",
            {"socketPath": socket_path, "hint": "reattach-unsupported"},
        )
    # Scan outbound payload (task text etc.) before it hits the socket.
    safe_payload = redact_secret_material(payload, redact_keys=True)
    if not isinstance(safe_payload, dict):
        raise GrokWrapperError("acp-failure", "control payload must be a JSON object")
    assert_no_secret_material(safe_payload)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((json.dumps(safe_payload, separators=(",", ":")) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(8192)
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        raise GrokWrapperError(
            "acp-failure",
            "control socket connect failed: {}".format(exc),
            {"socketPath": socket_path, "hint": "reattach-unsupported"},
        ) from exc
    finally:
        try:
            client.close()
        except OSError:
            pass
    if not buf.strip():
        raise GrokWrapperError("acp-failure", "empty control socket response")
    try:
        msg = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GrokWrapperError("acp-failure", "malformed control response: {}".format(exc)) from exc
    if msg.get("type") == "error":
        err = msg.get("error") or {}
        raise GrokWrapperError(
            str(err.get("class") or "acp-failure"),
            str(err.get("message") or "control plane error"),
        )
    env = msg.get("envelope")
    if not isinstance(env, dict):
        raise GrokWrapperError("acp-failure", "control response missing envelope")
    # Turn envelopes returned by peer-prompt must pass the same scan as stdout.
    assert_no_secret_material(env)
    return env


def serve_until_stop(
    session: Any,
    *,
    open_socket=open_control_socket,
    handle_connection=None,
    stop_session=None,
) -> dict:
    """Block serving the control socket until stop; return the final envelope."""
    if handle_connection is None or stop_session is None:
        raise GrokWrapperError("cli-failure", "serve_until_stop missing handlers")
    srv = open_socket(session.socket_path)
    final: Optional[dict] = None
    try:
        while final is None and not session._stop_requested:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                child = session.child
                if child is not None and getattr(child, "poll", lambda: None)() is not None:
                    session.peer_doc["lifecycle"] = "died"
                    try:
                        from groklib import runstate

                        runstate.write_json_atomic(
                            session.run_paths.run_dir / "peer.json", session.peer_doc
                        )
                    except OSError:
                        pass
                continue
            with conn:
                try:
                    final = handle_connection(session, conn)
                except GrokWrapperError as exc:
                    try:
                        write_json_line(
                            conn,
                            {
                                "type": "error",
                                "error": {"class": exc.error_class, "message": str(exc)},
                            },
                        )
                    except OSError:
                        pass
    finally:
        try:
            srv.close()
        except OSError:
            pass
        try:
            if session.socket_path.exists():
                session.socket_path.unlink()
        except OSError:
            pass
    if final is None:
        final = stop_session(session)
    return final
