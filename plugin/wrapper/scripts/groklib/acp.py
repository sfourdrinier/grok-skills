# wrapper/scripts/groklib/acp.py
#
# Experimental ACP (Agent Client Protocol) ndjson JSON-RPC 2.0 client over a
# child's stdio. Used by the peer channel (GROK_EXPERIMENTAL_ACP). Stdlib only;
# frame encode/decode is pure-testable; live I/O kills the child tree on timeout.

from __future__ import annotations

import json
import os
import select
import time
from typing import Any, Callable, Dict, Optional

from groklib import GrokWrapperError, log_stderr
from groklib import platformsupport

_PROTOCOL_VERSION = 1
_JSONRPC = "2.0"


def _log(function: str, message: str) -> None:
    log_stderr("acp", function, message)


def encode_frame(payload: Dict[str, Any]) -> bytes:
    """Encode one JSON-RPC object as a single ndjson line (trailing newline)."""
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_frame(raw: bytes) -> Dict[str, Any]:
    """Decode one ndjson JSON-RPC frame; raises acp-failure on malformed input."""
    if not isinstance(raw, (bytes, bytearray)):
        raise GrokWrapperError("acp-failure", "ACP frame must be bytes", {"type": type(raw).__name__})
    # Fail closed on undecodable bytes (matches the streaming F-STREAM-DECODE
    # invariant): errors="replace" would turn corruption into U+FFFD and let a
    # malformed frame parse as valid JSON.
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise GrokWrapperError(
            "acp-failure",
            "malformed ACP frame bytes (invalid UTF-8): {}".format(exc),
        ) from exc
    if not text:
        raise GrokWrapperError("acp-failure", "empty ACP frame")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GrokWrapperError(
            "acp-failure",
            "malformed ACP JSON-RPC frame: {}".format(exc),
        ) from exc
    if not isinstance(obj, dict):
        raise GrokWrapperError("acp-failure", "ACP frame must be a JSON object")
    return obj


class AcpClient:
    """JSON-RPC 2.0 client speaking ACP over a subprocess's stdin/stdout (ndjson)."""

    def __init__(self, proc: Any, timeout_seconds: float = 900.0) -> None:
        self._proc = proc
        self._timeout = float(timeout_seconds)
        self._next_id = 1
        self._stdin = proc.stdin
        self._stdout = proc.stdout
        # Internal line buffer filled via os.read (never mix select with
        # BufferedReader.readline - select sees an empty kernel buffer while
        # Python still holds the next frames).
        self._rx_buf = bytearray()
        if self._stdin is None or self._stdout is None:
            raise GrokWrapperError(
                "acp-failure",
                "ACP child is missing stdin/stdout pipes",
            )

    def _kill_tree(self) -> None:
        """Kill the ACP child without taking down this process group.

        Production spawns use ``start_new_session`` so killpg is safe. Test
        fakes often share the caller's process group; killpg would SIGKILL the
        test runner (and anything else in the group). Only killpg when the
        child lives in a different group.
        """
        proc = self._proc
        pid = getattr(proc, "pid", None)
        try:
            if (
                platformsupport.is_posix()
                and isinstance(pid, int)
                and pid > 0
            ):
                try:
                    child_pgid = os.getpgid(pid)
                    self_pgid = os.getpgrp()
                except OSError:
                    child_pgid = self_pgid = None
                if child_pgid is not None and child_pgid != self_pgid:
                    platformsupport.kill_process_tree(proc)
                else:
                    try:
                        proc.kill()
                    except OSError as exc:
                        _log("_kill_tree", "proc.kill failed: {}".format(exc))
            else:
                platformsupport.kill_process_tree(proc)
        except Exception as exc:  # best-effort
            _log("_kill_tree", "kill failed: {}".format(exc))
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def _raise_timeout(self, method: str) -> None:
        self._kill_tree()
        raise GrokWrapperError(
            "acp-failure",
            "ACP {} timed out after {}s".format(method, self._timeout),
            {"method": method, "timeoutSeconds": self._timeout},
        )

    def _write(self, payload: Dict[str, Any]) -> None:
        frame = encode_frame(payload)
        try:
            self._stdin.write(frame)
            self._stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._kill_tree()
            raise GrokWrapperError(
                "acp-failure",
                "failed writing ACP frame: {}".format(exc),
                {"method": payload.get("method")},
            ) from exc

    def _read_line(self, deadline: float, method: str) -> bytes:
        """Read one newline-delimited frame with a wall-clock deadline.

        Uses ``os.read`` on the pipe fd + an internal buffer so ``select`` and
        the reader share one view of pending bytes (never BufferedReader).
        """
        while b"\n" not in self._rx_buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._raise_timeout(method)
            fd = getattr(self._stdout, "fileno", lambda: -1)()
            if isinstance(fd, int) and fd >= 0:
                try:
                    ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
                except (ValueError, OSError):
                    ready = [fd]
                if not ready:
                    if self._proc.poll() is not None and not self._rx_buf:
                        raise GrokWrapperError(
                            "acp-failure",
                            "ACP child exited before responding to {}".format(method),
                            {"returncode": self._proc.returncode, "method": method},
                        )
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    self._kill_tree()
                    raise GrokWrapperError(
                        "acp-failure",
                        "failed reading ACP frame: {}".format(exc),
                        {"method": method},
                    ) from exc
            else:
                try:
                    chunk = self._stdout.read(4096)
                except (OSError, ValueError, TypeError) as exc:
                    self._kill_tree()
                    raise GrokWrapperError(
                        "acp-failure",
                        "failed reading ACP frame: {}".format(exc),
                        {"method": method},
                    ) from exc
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
            if chunk in (b"", None):
                if self._proc.poll() is not None:
                    if self._rx_buf:
                        break
                    raise GrokWrapperError(
                        "acp-failure",
                        "ACP child closed stdout during {}".format(method),
                        {"returncode": self._proc.returncode, "method": method},
                    )
                time.sleep(0.01)
                continue
            self._rx_buf.extend(chunk)
        if b"\n" in self._rx_buf:
            idx = self._rx_buf.index(b"\n")
            line = bytes(self._rx_buf[: idx + 1])
            del self._rx_buf[: idx + 1]
            return line
        line = bytes(self._rx_buf)
        self._rx_buf.clear()
        return line

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        on_notification: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        payload: Dict[str, Any] = {
            "jsonrpc": _JSONRPC,
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write(payload)
        deadline = time.monotonic() + self._timeout
        while True:
            raw = self._read_line(deadline, method)
            msg = decode_frame(raw)
            # Notifications (no id) may stream while a request is in flight.
            if "id" not in msg or msg.get("id") is None:
                if on_notification is not None:
                    on_notification(msg)
                continue
            if msg.get("id") != req_id:
                # Unrelated response; ignore (fail closed only on timeout/child death).
                _log("_request", "ignoring response id={} for method={}".format(msg.get("id"), method))
                continue
            if "error" in msg:
                err = msg.get("error") or {}
                raise GrokWrapperError(
                    "acp-failure",
                    "ACP {} error: {}".format(method, err.get("message") or err),
                    {"method": method, "error": err},
                )
            result = msg.get("result")
            if not isinstance(result, dict):
                # Some methods may return non-objects; normalize to dict.
                return {"value": result}
            return result

    def initialize(self) -> Dict[str, Any]:
        return self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "clientInfo": {"name": "grok-skills-wrapper", "version": "experimental"},
                "capabilities": {},
            },
        )

    def session_new(
        self,
        cwd: str,
        mcp_servers: Optional[list] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "cwd": cwd,
            "mcpServers": list(mcp_servers) if mcp_servers is not None else [],
        }
        params.update(extra)
        return self._request("session/new", params)

    def session_prompt(
        self,
        session_id: str,
        text: str,
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }
        params.update(extra)
        return self._request("session/prompt", params, on_notification=on_update)

    def session_cancel(self, session_id: str, **extra: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {"sessionId": session_id}
        params.update(extra)
        try:
            return self._request("session/cancel", params)
        except GrokWrapperError:
            # Cancel is best-effort during teardown.
            _log("session_cancel", "cancel failed (continuing teardown)")
            return {}

    def close(self) -> None:
        try:
            if self._stdin is not None:
                self._stdin.close()
        except Exception:
            pass
        self._kill_tree()
