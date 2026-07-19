# wrapper/scripts/tests/test_mode_peer_lifecycle_failclosed.py
#
# Residual peer lifecycle fail-closed contracts: durable envelope before terminal
# lifecycle, mandatory promptsHandled persistence before ACP, control-loop teardown
# on OSError/BaseException, missing startToken refuse, SIGTERM active-proc registry,
# and shared 4MiB frame caps for ACP + control.

from __future__ import annotations

import json
import os
import pathlib
import socket
import time
import unittest
from unittest import mock

from groklib import GrokWrapperError
from groklib import envelope as envelope_mod
from groklib import grokcli
from groklib import platformsupport
from groklib import runstate
from groklib.modes import peer as peer_mod
from groklib.modes import peer_control
from groklib.modes import peer_process
from groklib.modes import peer_stop
from tests.peer_test_base import PeerTestBase


class PeerLifecycleFailClosedTests(PeerTestBase):
    def _read_peer(self, run_paths: runstate.RunPaths) -> dict:
        return json.loads((run_paths.run_dir / "peer.json").read_text(encoding="utf-8"))

    def _terminal_success_env(self, run_id: str) -> dict:
        return envelope_mod.build_envelope(
            run_id=run_id,
            mode="peer-start",
            status="success",
            response={"peer": {"stopped": True}},
        )

    def test_finalize_and_mark_does_not_mark_terminal_without_durable_envelope(self) -> None:
        """stopped/failed peer.json only after durable envelope load succeeds under lock."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": os.getpid(),
            "startToken": "owner",
            "claimedAt": time.time(),
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        returned = self._terminal_success_env(run_paths.run_id)

        def _fake_finalize(**kwargs):
            # Production finalize may return success without durable when terminalize
            # fails; lifecycle owner must not write stopped/failed unless durable exists.
            return returned

        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(
                peer_stop, "load_durable_terminal_envelope", return_value=None
            ):
                env = peer_stop._finalize_and_mark(
                    run_paths=run_paths,
                    peer_doc=peer_doc,
                    home_path=pathlib.Path(peer_doc["homePath"]),
                    worktree=wt,
                    contract=None,
                    original_baseline=baseline,
                    stage=stage,
                )

        # Undurable success is fail-closed to failure for the caller, without
        # marking peer.json terminal (reclaimable stopping owner remains).
        self.assertEqual(env.get("status"), "failure")
        self.assertEqual(env.get("error", {}).get("class"), "state-ownership-violation")
        after = self._read_peer(run_paths)
        self.assertNotEqual(
            after.get("lifecycle"),
            "stopped",
            "must not mark stopped without durable terminal envelope on disk",
        )
        self.assertEqual(after.get("lifecycle"), "stopping")
        self.assertIn("stopOwner", after)

    def test_finalize_durable_success_survives_lifecycle_mark_failure(self) -> None:
        """Durable terminal success is returned even if mark_peer_lifecycle raises.

        Never synthesize a missing-durable failure when durable success already
        exists; leave peer.json reclaimable (stopping + stopOwner) for a later
        align/reclaim path.
        """
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": os.getpid(),
            "startToken": "owner-mark-fail",
            "claimedAt": time.time(),
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        durable = self._terminal_success_env(run_paths.run_id)

        def _fake_finalize(**kwargs):
            return dict(durable)

        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(
                peer_stop, "load_durable_terminal_envelope", return_value=durable
            ):
                with mock.patch.object(
                    peer_stop,
                    "mark_peer_lifecycle",
                    side_effect=OSError("peer.json lock poisoned"),
                ):
                    env = peer_stop._finalize_and_mark(
                        run_paths=run_paths,
                        peer_doc=peer_doc,
                        home_path=pathlib.Path(peer_doc["homePath"]),
                        worktree=wt,
                        contract=None,
                        original_baseline=baseline,
                        stage=stage,
                    )

        self.assertEqual(env.get("status"), "success")
        self.assertEqual(env.get("runId"), durable.get("runId"))
        self.assertNotEqual(
            env.get("error", {}).get("class"),
            "state-ownership-violation",
            "must not invent missing-durable failure when durable success exists",
        )
        after = self._read_peer(run_paths)
        self.assertEqual(after.get("lifecycle"), "stopping")
        self.assertIn("stopOwner", after)

    def test_finalize_exception_leaves_reclaimable_stopping_or_minimal_failed(self) -> None:
        """Exception/no durable leaves reclaimable stopping owner or minimal failed."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": os.getpid(),
            "startToken": "owner-token",
            "claimedAt": time.time(),
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=RuntimeError("finalize boom"),
        ):
            with self.assertRaises(RuntimeError):
                peer_stop._finalize_and_mark(
                    run_paths=run_paths,
                    peer_doc=peer_doc,
                    home_path=pathlib.Path(peer_doc["homePath"]),
                    worktree=wt,
                    contract=None,
                    original_baseline=baseline,
                    stage=stage,
                )

        after = self._read_peer(run_paths)
        life = after.get("lifecycle")
        # Either still reclaimable stopping (preferred) or durable-backed failed.
        if life == "stopping":
            self.assertIn("stopOwner", after)
            self.assertIsNone(peer_stop.load_durable_terminal_envelope(run_paths))
        else:
            self.assertEqual(life, "failed")
            durable = peer_stop.load_durable_terminal_envelope(run_paths)
            self.assertIsNotNone(durable)
            self.assertEqual(durable.get("status"), "failure")

    def test_prompts_handled_persist_failure_refuses_acp_prompt(self) -> None:
        """promptsHandled disk persistence is mandatory before ACP prompt (sentinel)."""
        from groklib.progress import ProgressWriter

        wt = pathlib.Path(self.tmp_root) / "prompt-persist-wt"
        wt.mkdir(exist_ok=True)
        run_paths = runstate.create_run("peer-start")
        peer_doc = {
            "schemaVersion": 1,
            "lifecycle": "running",
            "child": {"pid": os.getpid(), "startToken": "c"},
            "repoRoot": str(wt),
            "promptsHandled": 0,
            "leaseExpiresAt": time.time() + 100,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = __import__("threading").Lock()
        session._prompt_in_flight = False
        session.sentinel_name = ".grok-run-" + run_paths.run_id
        session.worktree = mock.Mock(path=wt)
        session.contract = None
        session.run_id = run_paths.run_id
        session.session_id = "s"
        session.progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        session.run_paths = run_paths
        session.model = "grok-4.5"
        session.child = mock.Mock(poll=mock.Mock(return_value=None))
        session.peer_doc = dict(peer_doc)
        session.home = mock.Mock(home_dir=pathlib.Path(self.tmp_root) / "h")
        session.home.home_dir.mkdir(exist_ok=True)
        session.acp = mock.Mock()
        session.acp.session_prompt = mock.Mock(return_value={"stopReason": "end_turn"})

        with mock.patch.object(runstate, "write_peer_lease"):
            with mock.patch(
                "groklib.modes.peer.patch_lease_expires",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(GrokWrapperError) as ctx:
                    peer_mod._handle_prompt(session, "task")

        self.assertEqual(ctx.exception.error_class, "acp-failure")
        session.acp.session_prompt.assert_not_called()
        after = self._read_peer(run_paths)
        # Disk must not show a successful prompt count when persist failed closed.
        self.assertEqual(int(after.get("promptsHandled") or 0), 0)
        # In-memory bump must roll back too: stop must not require the sentinel for a
        # prompt that never reached ACP.
        self.assertEqual(int(session.peer_doc.get("promptsHandled") or 0), 0)

    def test_serve_oserror_on_accept_tears_down_via_stop(self) -> None:
        """Control accept OSError must leave finally path that stops/tears down."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=mock.Mock(),
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=dict(peer_doc),
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )
        stop_calls = []

        def _stop(s):
            stop_calls.append(s.run_id)
            return self._terminal_success_env(s.run_id)

        class _Srv:
            def accept(self):
                raise OSError("accept failed: EMFILE")

            def close(self):
                return None

        final = peer_control.serve_until_stop(
            session,
            open_socket=lambda _p: _Srv(),
            handle_connection=lambda s, c: None,
            stop_session=_stop,
        )
        self.assertEqual(stop_calls, [run_paths.run_id])
        self.assertEqual(final.get("status"), "success")

    def test_serve_baseexception_tears_down_then_reraises_keyboardinterrupt(self) -> None:
        """BaseException (KeyboardInterrupt) must tear down then preserve interrupt."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=mock.Mock(),
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=dict(peer_doc),
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )
        stop_calls = []

        def _stop(s):
            stop_calls.append(s.run_id)
            return self._terminal_success_env(s.run_id)

        class _Srv:
            def accept(self):
                raise KeyboardInterrupt()

            def close(self):
                return None

        with self.assertRaises(KeyboardInterrupt):
            peer_control.serve_until_stop(
                session,
                open_socket=lambda _p: _Srv(),
                handle_connection=lambda s, c: None,
                stop_session=_stop,
            )
        self.assertEqual(
            stop_calls,
            [run_paths.run_id],
            "KeyboardInterrupt must still tear down peer resources via stop",
        )

    def test_start_refuses_null_or_empty_identity_tokens(self) -> None:
        """peer-start must refuse missing wrapper/child startToken (no fail-open)."""
        for bad in (None, ""):
            with self.subTest(token=bad):
                with mock.patch.object(
                    platformsupport, "process_start_token", return_value=bad
                ):
                    with self.assertRaises(GrokWrapperError) as ctx:
                        peer_mod._require_process_identity_token(42, role="child")
                    self.assertEqual(ctx.exception.error_class, "acp-failure")
                    self.assertIn("startToken", str(ctx.exception))

    def test_wrapper_still_live_treats_empty_token_live_pid_as_live(self) -> None:
        """Unverifiable live wrapper pid must be treated live (refuse finalize/kill)."""
        doc = {"wrapper": {"pid": 515151, "startToken": ""}}
        with mock.patch.object(platformsupport, "process_is_alive", return_value=True):
            with mock.patch.object(platformsupport, "process_start_token", return_value=None):
                self.assertTrue(peer_stop.wrapper_still_live(doc))
        with mock.patch.object(platformsupport, "process_is_alive", return_value=True):
            with mock.patch.object(
                platformsupport, "kill_process_tree_by_pid"
            ) as kill:
                attempted = peer_stop.kill_recorded_wrapper(doc)
        self.assertFalse(attempted)
        kill.assert_not_called()

    def test_connect_control_rejects_oversized_response(self) -> None:
        """connect_control must cap response frames (shared 4MiB SSOT)."""
        from groklib import acp as acp_mod

        cap = acp_mod.MAX_FRAME_BYTES
        self.assertEqual(cap, 4 * 1024 * 1024)
        self.assertEqual(peer_control.MAX_FRAME_BYTES, cap)

        # Short AF_UNIX path (nested XDG temp roots exceed ~104-byte limit).
        import tempfile

        sock_dir = pathlib.Path(tempfile.mkdtemp(prefix="gctl-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(sock_dir), ignore_errors=True))
        sock_path = sock_dir / "c.sock"
        if sock_path.exists():
            sock_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(1)
        self.addCleanup(srv.close)

        def _serve():
            conn, _ = srv.accept()
            try:
                conn.recv(4096)
                # No newline until past the cap so the client aborts on size.
                conn.sendall(b"x" * (cap + 64))
            finally:
                conn.close()

        import threading

        t = threading.Thread(target=_serve)
        t.daemon = True
        t.start()
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_control.connect_control(str(sock_path), {"op": "prompt", "task": "x"}, timeout=5.0)
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        self.assertIn("too large", str(ctx.exception).lower())
        t.join(timeout=2)

    def test_acp_client_rejects_oversized_frame(self) -> None:
        """AcpClient must refuse frames larger than shared MAX_FRAME_BYTES."""
        from groklib import acp as acp_mod

        class _Stdout:
            def __init__(self, payload: bytes):
                self._payload = payload
                self._sent = False

            def fileno(self):
                return -1

            def read(self, _n):
                if self._sent:
                    return b""
                self._sent = True
                return self._payload

        class _Proc:
            stdin = mock.Mock()
            stdout = None
            returncode = None

            def poll(self):
                return None

            def kill(self):
                return None

            def wait(self, timeout=None):
                return 0

        huge = b"{" + (b"a" * (acp_mod.MAX_FRAME_BYTES + 8)) + b"}\n"
        proc = _Proc()
        proc.stdout = _Stdout(huge)
        proc.stdin = mock.Mock()
        client = acp_mod.AcpClient(proc, timeout_seconds=2)
        with self.assertRaises(GrokWrapperError) as ctx:
            client._read_line(time.monotonic() + 2.0, "initialize")
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        self.assertIn("too large", str(ctx.exception).lower())

    def test_spawn_acp_child_registers_for_sigterm_and_unregister_on_kill(self) -> None:
        """ACP child must join SIGTERM active-proc registry; kill path unregisters."""
        home = mock.Mock()
        home.home_dir = pathlib.Path(self.tmp_root) / "reg-home"
        home.home_dir.mkdir(exist_ok=True)
        worktree = mock.Mock(path=pathlib.Path(self.tmp_root) / "reg-wt")
        worktree.path.mkdir(exist_ok=True)
        fake = mock.Mock(pid=424299)

        with mock.patch.object(peer_process.subprocess, "Popen", return_value=fake):
            with mock.patch.object(
                peer_process.platformsupport, "spawn_kwargs_new_group", return_value={}
            ):
                with mock.patch.object(peer_process.grokcli, "_minimal_env", return_value={"HOME": "h", "PATH": "p", "TMPDIR": "t"}):
                    with mock.patch.object(grokcli, "_register_active_proc") as reg:
                        proc = peer_process.spawn_acp_child(
                            binary=pathlib.Path("/usr/bin/true"),
                            home=home,
                            worktree=worktree,
                            leader_socket=pathlib.Path(self.tmp_root) / "s.sock",
                            model="grok-4.5",
                            policy=mock.Mock(profile="workspace"),
                            tools=("read",),
                            web_access=False,
                        )
        reg.assert_called_once_with(fake)
        self.assertIs(proc, fake)

        doc = {"child": {"pid": 424299, "startToken": "tok"}}
        with mock.patch.object(platformsupport, "process_is_alive", return_value=True):
            with mock.patch.object(platformsupport, "process_start_token", return_value="tok"):
                with mock.patch.object(platformsupport, "is_posix", return_value=True):
                    with mock.patch.object(
                        peer_process.os, "getpgid", side_effect=lambda pid: 1 if pid == 424299 else 2
                    ):
                        with mock.patch.object(
                            platformsupport, "kill_process_tree_by_pid"
                        ) as kill:
                            with mock.patch.object(
                                peer_process, "unregister_active_child"
                            ) as unreg:
                                peer_process.kill_recorded_child(doc)
        kill.assert_called_once_with(424299)
        unreg.assert_called()


if __name__ == "__main__":
    unittest.main()
