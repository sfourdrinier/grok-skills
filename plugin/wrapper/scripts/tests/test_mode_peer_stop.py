# wrapper/scripts/tests/test_mode_peer_stop.py
#
# Peer-stop lifecycle ownership: abandoned stopping reclaim, concurrent field
# preservation, failure -> failed (not stopped), idempotent restop, concurrent
# single finalize, control-response write failure must not re-finalize,
# live-wrapper fallback safety, and fresh empty stop-stage fields.

from __future__ import annotations

import json
import pathlib
import socket
import threading
import time
import unittest
from unittest import mock

from groklib import GrokWrapperError
from groklib import envelope as envelope_mod
from groklib import platformsupport
from groklib import runstate
from groklib.modes import peer as peer_mod
from groklib.modes import peer_control
from groklib.modes import peer_stop
from tests.peer_test_base import PeerTestBase


class PeerStopLifecycleTests(PeerTestBase):
    def _terminal_success_env(self, run_id: str) -> dict:
        # Durable terminalize rewrites mode to peer-start for run-record binding.
        return envelope_mod.build_envelope(
            run_id=run_id,
            mode="peer-start",
            status="success",
            response={"peer": {"stopped": True}},
        )

    def _terminal_failure_env(self, run_id: str) -> dict:
        return envelope_mod.failure_envelope(
            run_id=run_id,
            mode="peer-stop",
            error_class="acp-failure",
            message="simulated peer finalize failure",
        )

    def _persist_terminal(
        self, run_paths: runstate.RunPaths, env: dict, *, lifecycle: str = "completed"
    ) -> None:
        rec = runstate.load_run_record(run_paths.run_id)
        rev = int(rec.get("recordRevision", 0))
        life = rec.get("lifecycle")
        if life == "created":
            rec = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(rec["recordRevision"])
            life = "running"
        if life == "running" and lifecycle == "completed":
            rec = runstate.set_lifecycle(run_paths, rev, "finalizing")
            rev = int(rec["recordRevision"])
        durable = dict(env)
        durable["mode"] = "peer-start"
        runstate.persist_terminal_envelope(
            run_paths, rev, durable, lifecycle=lifecycle
        )

    def _read_peer(self, run_paths: runstate.RunPaths) -> dict:
        return json.loads((run_paths.run_dir / "peer.json").read_text(encoding="utf-8"))

    def test_abandoned_stopping_reclaims_after_grace(self) -> None:
        """Seeded stopping with dead stopper must not poison; reclaim + finalize."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["socketPath"] = str(run_paths.run_dir / "missing.sock")
        peer_doc["lifecycle"] = "stopping"
        # Dead/abandoned stopper evidence: recycled pid token, old claim time.
        peer_doc["stopOwner"] = {
            "pid": 424242,
            "startToken": "dead-stopper-token",
            "claimedAt": time.time() - 3600.0,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        finalize_calls = []

        def _fake_finalize(**kwargs):
            finalize_calls.append(kwargs)
            env = self._terminal_success_env(run_paths.run_id)
            try:
                self._persist_terminal(run_paths, env)
            except Exception:
                pass
            return env

        def _alive(pid):
            return False

        def _token(pid):
            return None

        ns = mock.Mock(run_id=run_paths.run_id)
        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(peer_stop, "kill_recorded_child"):
                with mock.patch.object(peer_stop, "ensure_wrapper_down_for_fallback"):
                    with mock.patch.object(
                        platformsupport, "process_is_alive", side_effect=_alive
                    ):
                        with mock.patch.object(
                            platformsupport, "process_start_token", side_effect=_token
                        ):
                            with mock.patch.object(
                                peer_stop, "_PEER_STOP_OWNER_GRACE_SECONDS", 0.0
                            ):
                                env = peer_mod.run_peer_stop(ns)

        self.assertEqual(env.get("status"), "success")
        self.assertEqual(len(finalize_calls), 1)
        final_doc = self._read_peer(run_paths)
        self.assertEqual(final_doc.get("lifecycle"), "stopped")
        self.assertNotIn("stopOwner", final_doc)

    def test_mark_peer_lifecycle_does_not_clobber_concurrent_fields(self) -> None:
        """Lifecycle mutations re-read under lock and patch only ownership fields."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        on_disk = dict(peer_doc)
        on_disk["lifecycle"] = "stopping"
        on_disk["promptsHandled"] = 7
        on_disk["leaseExpiresAt"] = 9999.0
        on_disk["concurrentField"] = "live-value"
        on_disk["stopOwner"] = {
            "pid": 1,
            "startToken": "old",
            "claimedAt": time.time() - 10,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", on_disk)

        stale_base = {
            "schemaVersion": 1,
            "lifecycle": "stopping",
            "promptsHandled": 0,
            "leaseExpiresAt": 1.0,
            "concurrentField": "stale-value",
            "onlyInStale": True,
            "stopOwner": {"pid": 1, "startToken": "old", "claimedAt": 0},
        }
        updated = peer_stop.mark_peer_lifecycle(
            run_paths, "stopped", base_doc=stale_base
        )
        self.assertEqual(updated.get("lifecycle"), "stopped")
        self.assertEqual(updated.get("promptsHandled"), 7)
        self.assertEqual(updated.get("leaseExpiresAt"), 9999.0)
        self.assertEqual(updated.get("concurrentField"), "live-value")
        self.assertNotIn("onlyInStale", updated)
        self.assertNotIn("stopOwner", updated)

        reread = self._read_peer(run_paths)
        self.assertEqual(reread.get("lifecycle"), "stopped")
        self.assertEqual(reread.get("promptsHandled"), 7)
        self.assertEqual(reread.get("concurrentField"), "live-value")
        self.assertNotIn("onlyInStale", reread)
        self.assertNotIn("stopOwner", reread)

    def test_failure_envelope_sets_lifecycle_failed_not_stopped(self) -> None:
        """Failure terminal must end peer lifecycle=failed, never stopped."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["socketPath"] = str(run_paths.run_dir / "missing.sock")
        peer_doc["lifecycle"] = "running"
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        def _fake_finalize(**kwargs):
            env = self._terminal_failure_env(run_paths.run_id)
            try:
                self._persist_terminal(run_paths, env, lifecycle="failed")
            except Exception:
                pass
            return env

        ns = mock.Mock(run_id=run_paths.run_id)
        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(peer_stop, "kill_recorded_child"):
                with mock.patch.object(peer_stop, "ensure_wrapper_down_for_fallback"):
                    env = peer_mod.run_peer_stop(ns)

        self.assertEqual(env.get("status"), "failure")
        final_doc = self._read_peer(run_paths)
        self.assertEqual(
            final_doc.get("lifecycle"),
            "failed",
            "failure envelope must set peer lifecycle=failed, not stopped",
        )
        self.assertNotEqual(final_doc.get("lifecycle"), "stopped")

    def test_finalize_failure_path_does_not_write_stopped_before_owner(self) -> None:
        """peer_finalize must not force lifecycle=stopped on failure envelopes."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        from groklib.modes import peer_finalize

        with mock.patch.object(
            peer_finalize,
            "code_handoff_finalize",
            side_effect=GrokWrapperError("acp-failure", "handoff boom"),
        ):
            with mock.patch.object(
                peer_finalize,
                "destroy_private_home",
                return_value={"status": "clean", "detail": None},
            ):
                env = peer_finalize.finalize_peer_session(
                    run_paths=run_paths,
                    peer_doc=peer_doc,
                    home_path=pathlib.Path(peer_doc["homePath"]),
                    worktree=wt,
                    contract=None,
                    original_baseline=baseline,
                    stage=stage,
                )

        self.assertEqual(env.get("status"), "failure")
        after = self._read_peer(run_paths)
        # Single terminal owner is peer_stop; finalize must not write stopped.
        self.assertNotEqual(after.get("lifecycle"), "stopped")
        self.assertEqual(after.get("lifecycle"), "stopping")

    def test_sequential_restop_returns_same_terminal_without_second_finalize(self) -> None:
        """Durable terminal envelope is returned idempotently; no re-finalize."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        terminal = self._terminal_success_env(run_paths.run_id)
        self._persist_terminal(run_paths, terminal)
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopped"
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        finalize_calls = []

        def _fake_finalize(**kwargs):
            finalize_calls.append(kwargs)
            return self._terminal_success_env(run_paths.run_id)

        ns = mock.Mock(run_id=run_paths.run_id)
        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            first = peer_mod.run_peer_stop(ns)
            second = peer_mod.run_peer_stop(ns)

        self.assertEqual(first.get("runId"), run_paths.run_id)
        self.assertEqual(second.get("runId"), run_paths.run_id)
        self.assertEqual(first.get("status"), "success")
        self.assertEqual(second.get("status"), "success")
        self.assertEqual(first.get("response"), second.get("response"))
        self.assertEqual(
            finalize_calls,
            [],
            "restop with durable terminal must not re-enter finalize",
        )
        # Home must not be torn down on idempotent restop.
        self.assertTrue(pathlib.Path(home.home_dir).exists())

    def test_concurrent_dual_stop_single_finalize(self) -> None:
        """Two concurrent peer-stop callers: exactly one finalize, same outcome."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["socketPath"] = str(run_paths.run_dir / "missing.sock")
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        finalize_calls = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        release_finalize = threading.Event()

        def _fake_finalize(**kwargs):
            with lock:
                finalize_calls.append(1)
            # Hold so the second stopper observes lifecycle=stopping.
            release_finalize.wait(timeout=2)
            env = self._terminal_success_env(run_paths.run_id)
            # Mirror production terminalize so the waiter can return durable.
            try:
                self._persist_terminal(run_paths, env)
            except Exception:
                pass
            return env

        results = [None, None]
        errors = [None, None]

        def _worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                results[idx] = peer_mod.run_peer_stop(mock.Mock(run_id=run_paths.run_id))
            except Exception as exc:  # noqa: BLE001 - collect for assertion
                errors[idx] = exc

        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(peer_mod, "_kill_recorded_child"):
                with mock.patch.object(peer_stop, "kill_recorded_child"):
                    with mock.patch.object(peer_stop, "ensure_wrapper_down_for_fallback"):
                        # Keep the live owner non-reclaimable while finalize holds.
                        with mock.patch.object(
                            peer_stop, "_stop_owner_is_live", return_value=True
                        ):
                            threads = [
                                threading.Thread(target=_worker, args=(0,)),
                                threading.Thread(target=_worker, args=(1,)),
                            ]
                            for t in threads:
                                t.start()
                            # Let both race the claim, then release the owner finalize.
                            time.sleep(0.1)
                            release_finalize.set()
                            for t in threads:
                                t.join(timeout=15)

        self.assertEqual(errors, [None, None], "neither stopper may raise: {}".format(errors))
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertEqual(results[0].get("status"), "success")
        self.assertEqual(results[1].get("status"), "success")
        self.assertEqual(
            sum(finalize_calls),
            1,
            "exactly one concurrent stopper may run finalize",
        )

    def test_control_write_failure_after_finalize_does_not_restop(self) -> None:
        """write_json_line GrokWrapperError after stop finalize must not re-stop."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        stop_calls = []

        def _stop(session):
            stop_calls.append(session.run_id)
            return self._terminal_success_env(session.run_id)

        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=None,
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=peer_doc,
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, *_a, **_k):
                return None

            def getsockopt(self, *a, **k):
                raise OSError("no creds")

            def recv(self, _n):
                if getattr(self, "_sent", False):
                    return b""
                self._sent = True
                return b'{"op":"stop"}\n'

            def sendall(self, data):
                return None

        conn = _Conn()
        write_calls = {"n": 0}

        def _write_fail(c, obj):
            write_calls["n"] += 1
            raise GrokWrapperError(
                "output-malformed",
                "simulated post-finalize control write failure",
            )

        def _open(_path):
            srv = mock.Mock()
            state = {"accepted": False}

            def _accept_timeout():
                if not state["accepted"]:
                    state["accepted"] = True
                    return conn, None
                raise socket.timeout()

            srv.accept = _accept_timeout
            srv.close = mock.Mock()
            return srv

        with mock.patch.object(peer_control, "write_json_line", side_effect=_write_fail):
            with mock.patch.object(peer_mod, "_stop_session", side_effect=_stop):
                final = peer_control.serve_until_stop(
                    session,
                    open_socket=_open,
                    handle_connection=peer_mod._handle_control_connection,
                    stop_session=_stop,
                )

        self.assertEqual(final.get("status"), "success")
        self.assertEqual(
            stop_calls,
            [run_paths.run_id],
            "control write failure after finalize must not call stop/finalize again",
        )
        self.assertEqual(write_calls["n"], 1)

    def test_control_error_with_live_wrapper_kills_or_refuses_local_finalize(self) -> None:
        """Socket stop failure while wrapper still live must not local-finalize alone."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        sock_path = run_paths.run_dir / "peer.sock"
        sock_path.write_text("", encoding="utf-8")
        wrapper_pid = 424242
        wrapper_token = "wrapper-start-token-unique"
        peer_doc = dict(peer_doc)
        peer_doc["socketPath"] = str(sock_path)
        peer_doc["wrapper"] = {"pid": wrapper_pid, "startToken": wrapper_token}
        peer_doc["lifecycle"] = "running"
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        finalize_calls = []
        killed = []

        def _fake_finalize(**kwargs):
            finalize_calls.append(kwargs)
            env = self._terminal_success_env(run_paths.run_id)
            # Durable terminal is required before peer.json may become stopped.
            try:
                self._persist_terminal(run_paths, env)
            except Exception:
                pass
            return env

        def _connect_fail(socket_path, payload, timeout=900.0):
            raise GrokWrapperError("acp-failure", "control socket connect failed")

        def _alive(pid):
            return pid == wrapper_pid and wrapper_pid not in killed

        def _token(pid):
            if pid == wrapper_pid:
                return wrapper_token
            return None

        def _kill_pid(pid):
            killed.append(pid)

        ns = mock.Mock(run_id=run_paths.run_id)
        with mock.patch.object(peer_mod, "_connect_control", side_effect=_connect_fail):
            with mock.patch.object(peer_stop, "kill_recorded_child"):
                with mock.patch(
                    "groklib.modes.peer_finalize.finalize_peer_session",
                    side_effect=_fake_finalize,
                ):
                    with mock.patch.object(
                        platformsupport, "process_is_alive", side_effect=_alive
                    ):
                        with mock.patch.object(
                            platformsupport, "process_start_token", side_effect=_token
                        ):
                            with mock.patch.object(
                                platformsupport,
                                "kill_process_tree_by_pid",
                                side_effect=_kill_pid,
                            ):
                                try:
                                    env = peer_mod.run_peer_stop(ns)
                                except GrokWrapperError as exc:
                                    self.assertEqual(finalize_calls, [])
                                    self.assertIn(
                                        exc.error_class,
                                        ("acp-failure", "state-ownership-violation"),
                                    )
                                    return
        self.assertIn(wrapper_pid, killed)
        self.assertEqual(len(finalize_calls), 1)
        self.assertEqual(env.get("status"), "success")

    def test_fallback_stage_lists_are_instance_not_shared(self) -> None:
        """Two empty stop stages must not share mutable list fields."""
        stage_a = peer_stop.build_empty_stop_stage(
            run_id="run-a",
            worktree_path=pathlib.Path("/tmp/a"),
            session_id="s1",
            progress=mock.Mock(),
        )
        stage_b = peer_stop.build_empty_stop_stage(
            run_id="run-b",
            worktree_path=pathlib.Path("/tmp/b"),
            session_id="s2",
            progress=mock.Mock(),
        )
        stage_a.acc.commands.append({"cmd": "a"})
        stage_a.acc.changed_files.append("a.txt")
        stage_a.acc.warnings.append("w-a")
        self.assertEqual(stage_b.acc.commands, [])
        self.assertEqual(stage_b.acc.changed_files, [])
        self.assertEqual(stage_b.acc.warnings, [])
        self.assertIsNot(stage_a.acc.commands, stage_b.acc.commands)
        self.assertIsNot(stage_a.acc.changed_files, stage_b.acc.changed_files)
        self.assertIsNot(stage_a.acc.warnings, stage_b.acc.warnings)

    def test_never_steal_from_live_verified_stop_owner(self) -> None:
        """A live stop owner must not be reclaimed by a second stopper."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 777001
        owner_token = "live-stop-owner-token"
        peer_doc = dict(peer_doc)
        peer_doc["socketPath"] = str(run_paths.run_dir / "missing.sock")
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": owner_pid,
            "startToken": owner_token,
            "claimedAt": time.time(),
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        finalize_calls = []

        def _fake_finalize(**kwargs):
            finalize_calls.append(1)
            return self._terminal_success_env(run_paths.run_id)

        def _alive(pid):
            return pid == owner_pid

        def _token(pid):
            return owner_token if pid == owner_pid else None

        ns = mock.Mock(run_id=run_paths.run_id)
        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(peer_stop, "kill_recorded_child"):
                with mock.patch.object(peer_stop, "ensure_wrapper_down_for_fallback"):
                    with mock.patch.object(
                        platformsupport, "process_is_alive", side_effect=_alive
                    ):
                        with mock.patch.object(
                            platformsupport, "process_start_token", side_effect=_token
                        ):
                            with mock.patch.object(
                                peer_stop, "_PEER_STOP_CLAIM_WAIT_SECONDS", 0.2
                            ):
                                with self.assertRaises(GrokWrapperError) as ctx:
                                    peer_mod.run_peer_stop(ns)

        self.assertEqual(finalize_calls, [])
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        self.assertIn("timed out", str(ctx.exception).lower())
        still = self._read_peer(run_paths)
        self.assertEqual(still.get("lifecycle"), "stopping")
        self.assertEqual((still.get("stopOwner") or {}).get("pid"), owner_pid)


if __name__ == "__main__":
    unittest.main()
