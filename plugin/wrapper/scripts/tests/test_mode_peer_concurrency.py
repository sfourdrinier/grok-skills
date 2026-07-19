# wrapper/scripts/tests/test_mode_peer_concurrency.py
#
# Residual peer.json concurrency defects: renew_lease / child-death whole-doc
# writes must not clobber stopping+stopOwner; empty startToken fail-closed; peer
# prompt lifecycle fail-closed beyond missing socket.

from __future__ import annotations

import json
import os
import pathlib
import time
import unittest
from unittest import mock

from groklib import GrokWrapperError
from groklib import platformsupport
from groklib import runstate
from groklib.modes import peer as peer_mod
from groklib.modes import peer_control
from groklib.modes import peer_stop
from tests.peer_test_base import PeerTestBase


class PeerConcurrencyResidualTests(PeerTestBase):
    def _read_peer(self, run_paths: runstate.RunPaths) -> dict:
        return json.loads((run_paths.run_dir / "peer.json").read_text(encoding="utf-8"))

    def _seed_stopping_doc(self, run_paths, peer_doc, *, owner_pid=777777, owner_token="live-owner"):
        on_disk = dict(peer_doc)
        on_disk["lifecycle"] = "stopping"
        on_disk["stopOwner"] = {
            "pid": owner_pid,
            "startToken": owner_token,
            "claimedAt": time.time(),
        }
        on_disk["promptsHandled"] = 3
        on_disk["concurrentField"] = "stop-claimed"
        on_disk["leaseExpiresAt"] = 1111.0
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", on_disk)
        return on_disk

    def test_renew_lease_does_not_overwrite_stopping_or_stop_owner(self) -> None:
        """Stale resident peer_doc renew must field-patch lease only (no dual-finalize)."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 888001
        owner_token = "stop-owner-token-live"
        on_disk = self._seed_stopping_doc(
            run_paths, peer_doc, owner_pid=owner_pid, owner_token=owner_token
        )

        # Resident holds a STALE whole doc from before peer-stop claimed stopping.
        stale = dict(peer_doc)
        stale["lifecycle"] = "running"
        stale.pop("stopOwner", None)
        stale["promptsHandled"] = 0
        stale["concurrentField"] = "stale-resident"
        stale["leaseExpiresAt"] = 1.0

        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=mock.Mock(pid=os.getpid()),
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=stale,
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )
        with mock.patch.object(runstate, "write_peer_lease"):
            session.renew_lease()

        after = self._read_peer(run_paths)
        self.assertEqual(
            after.get("lifecycle"),
            "stopping",
            "renew_lease must not reopen dual-finalize by restoring lifecycle=running",
        )
        stop_owner = after.get("stopOwner") or {}
        self.assertEqual(stop_owner.get("pid"), owner_pid)
        self.assertEqual(stop_owner.get("startToken"), owner_token)
        self.assertEqual(after.get("promptsHandled"), 3)
        self.assertEqual(after.get("concurrentField"), "stop-claimed")
        self.assertNotEqual(after.get("leaseExpiresAt"), 1111.0)
        self.assertGreater(float(after.get("leaseExpiresAt") or 0), time.time())
        self.assertNotIn("onlyInStale", after)
        # On-disk must remain the stop claim, not the stale resident snapshot.
        self.assertNotEqual(after.get("lifecycle"), "running")
        self.assertEqual(on_disk.get("lifecycle"), "stopping")

    def test_renew_lease_uses_max_prompts_handled_ssot(self) -> None:
        """renew_lease / patch_lease_expires raise promptsHandled via peer_doc SSOT."""
        from groklib import peer_doc as peer_doc_mod

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        on_disk = dict(peer_doc)
        on_disk["lifecycle"] = "running"
        on_disk["promptsHandled"] = 4
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", on_disk)

        mem = dict(peer_doc)
        mem["lifecycle"] = "running"
        mem["promptsHandled"] = 7
        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=mock.Mock(pid=os.getpid()),
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=mem,
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )

        with mock.patch.object(runstate, "write_peer_lease"):
            with mock.patch.object(
                peer_doc_mod,
                "max_prompts_handled",
                wraps=peer_doc_mod.max_prompts_handled,
            ) as max_ssot:
                session.renew_lease()

        self.assertTrue(
            max_ssot.called,
            "renew_lease must sync promptsHandled via peer_doc.max_prompts_handled",
        )
        after = self._read_peer(run_paths)
        self.assertEqual(after.get("promptsHandled"), 7)
        self.assertEqual(session.peer_doc.get("promptsHandled"), 7)

        # Disk higher than memory still uses the shared max SSOT.
        on_disk2 = self._read_peer(run_paths)
        on_disk2["promptsHandled"] = 11
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", on_disk2)
        session.peer_doc["promptsHandled"] = 5
        with mock.patch.object(runstate, "write_peer_lease"):
            with mock.patch.object(
                peer_doc_mod,
                "max_prompts_handled",
                wraps=peer_doc_mod.max_prompts_handled,
            ) as max_ssot2:
                session.renew_lease()
        self.assertTrue(max_ssot2.called)
        after2 = self._read_peer(run_paths)
        self.assertEqual(after2.get("promptsHandled"), 11)
        self.assertEqual(session.peer_doc.get("promptsHandled"), 11)

    def test_child_death_write_does_not_clobber_stopping_stop_owner(self) -> None:
        """Death lifecycle write racing stop must preserve stopping ownership."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 888002
        owner_token = "stop-owner-during-death"
        self._seed_stopping_doc(
            run_paths, peer_doc, owner_pid=owner_pid, owner_token=owner_token
        )

        stale = dict(peer_doc)
        stale["lifecycle"] = "running"
        stale.pop("stopOwner", None)
        stale["promptsHandled"] = 0
        stale["concurrentField"] = "stale-death"

        dead_child = mock.Mock()
        dead_child.poll = mock.Mock(return_value=1)
        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=dead_child,
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=stale,
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod._handle_prompt(session, "task")
        self.assertEqual(ctx.exception.error_class, "acp-failure")

        after = self._read_peer(run_paths)
        self.assertEqual(
            after.get("lifecycle"),
            "stopping",
            "child-death must not overwrite lifecycle=stopping with died",
        )
        stop_owner = after.get("stopOwner") or {}
        self.assertEqual(stop_owner.get("pid"), owner_pid)
        self.assertEqual(stop_owner.get("startToken"), owner_token)
        self.assertEqual(after.get("promptsHandled"), 3)
        self.assertEqual(after.get("concurrentField"), "stop-claimed")

    def test_serve_child_death_write_preserves_stopping(self) -> None:
        """peer_control serve death path must field-patch under lock with guards."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 888003
        owner_token = "serve-death-owner"
        self._seed_stopping_doc(
            run_paths, peer_doc, owner_pid=owner_pid, owner_token=owner_token
        )
        stale = dict(peer_doc)
        stale["lifecycle"] = "running"
        stale.pop("stopOwner", None)

        dead_child = mock.Mock()
        dead_child.poll = mock.Mock(return_value=9)
        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(),
            child=dead_child,
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=stale,
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )

        class _Srv:
            def __init__(self):
                self.n = 0

            def accept(self):
                self.n += 1
                if self.n == 1:
                    raise socket_timeout()
                session._stop_requested = True
                raise socket_timeout()

            def close(self):
                return None

        def socket_timeout():
            raise __import__("socket").timeout()

        def _open(_path):
            return _Srv()

        peer_control.serve_until_stop(
            session,
            open_socket=_open,
            handle_connection=lambda s, c: None,
            stop_session=lambda s: {"status": "success", "runId": s.run_id, "mode": "peer-stop"},
        )
        after = self._read_peer(run_paths)
        self.assertEqual(after.get("lifecycle"), "stopping")
        self.assertEqual((after.get("stopOwner") or {}).get("pid"), owner_pid)
        self.assertEqual((after.get("stopOwner") or {}).get("startToken"), owner_token)

    def test_empty_stop_owner_token_with_live_pid_not_reclaimed(self) -> None:
        """Alive stopOwner PID with empty/unavailable startToken must not be stolen."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 999001
        peer_doc = dict(peer_doc)
        peer_doc["socketPath"] = str(run_paths.run_dir / "missing.sock")
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": owner_pid,
            "startToken": "",
            "claimedAt": time.time() - 3600.0,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        finalize_calls = []

        def _fake_finalize(**kwargs):
            finalize_calls.append(1)
            return {"status": "success", "runId": run_paths.run_id, "mode": "peer-start"}

        def _alive(pid):
            return pid == owner_pid

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
                                peer_stop, "_PEER_STOP_CLAIM_WAIT_SECONDS", 0.15
                            ):
                                with mock.patch.object(
                                    peer_stop, "_PEER_STOP_OWNER_GRACE_SECONDS", 0.0
                                ):
                                    with self.assertRaises(GrokWrapperError) as ctx:
                                        peer_mod.run_peer_stop(ns)

        self.assertEqual(finalize_calls, [])
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        still = self._read_peer(run_paths)
        self.assertEqual(still.get("lifecycle"), "stopping")
        self.assertEqual((still.get("stopOwner") or {}).get("pid"), owner_pid)

    def test_missing_stop_owner_token_key_with_live_pid_not_reclaimed(self) -> None:
        """Alive stopOwner with missing startToken key is fail-closed (not safely dead)."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 999002
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": owner_pid,
            "claimedAt": time.time() - 100.0,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        def _alive(pid):
            return pid == owner_pid

        with mock.patch.object(platformsupport, "process_is_alive", side_effect=_alive):
            with mock.patch.object(platformsupport, "process_start_token", return_value=None):
                with mock.patch.object(peer_stop, "_PEER_STOP_OWNER_GRACE_SECONDS", 0.0):
                    outcome, doc, durable = peer_stop.claim_peer_stop(run_paths)
        self.assertEqual(outcome, "wait")
        self.assertIsNone(durable)
        self.assertEqual(doc.get("lifecycle"), "stopping")
        self.assertEqual((doc.get("stopOwner") or {}).get("pid"), owner_pid)

    def test_wrong_token_pid_reuse_does_not_treat_recycled_as_live_owner(self) -> None:
        """Recycled PID with mismatched startToken is not the stop owner (reclaim ok)."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 999003
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": owner_pid,
            "startToken": "original-owner-token",
            "claimedAt": time.time() - 3600.0,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        def _alive(pid):
            return pid == owner_pid

        def _token(pid):
            # Recycled owner pid has a different token; this process must still
            # expose a non-empty identity so the reclaim claim can fail closed on
            # missing tokens rather than poisoning stopOwner.
            if pid == owner_pid:
                return "recycled-different-token"
            if pid == os.getpid():
                return "reclaimer-token"
            return None

        with mock.patch.object(platformsupport, "process_is_alive", side_effect=_alive):
            with mock.patch.object(platformsupport, "process_start_token", side_effect=_token):
                with mock.patch.object(peer_stop, "_PEER_STOP_OWNER_GRACE_SECONDS", 0.0):
                    outcome, doc, durable = peer_stop.claim_peer_stop(run_paths)
        self.assertEqual(outcome, "claimed")
        self.assertIsNone(durable)
        self.assertEqual(doc.get("lifecycle"), "stopping")
        # New claim owner is this process, not the recycled pid.
        self.assertNotEqual((doc.get("stopOwner") or {}).get("startToken"), "original-owner-token")
        self.assertEqual((doc.get("stopOwner") or {}).get("startToken"), "reclaimer-token")
        self.assertNotEqual((doc.get("stopOwner") or {}).get("pid"), owner_pid)

    def test_kill_recorded_wrapper_refuses_wrong_token_pid_reuse(self) -> None:
        """kill_recorded_wrapper must refuse to kill a recycled pid with wrong token."""
        doc = {
            "wrapper": {"pid": 424200, "startToken": "original-wrapper-token"},
        }
        killed = []

        def _alive(pid):
            return pid == 424200

        def _token(pid):
            return "recycled-wrapper-token" if pid == 424200 else None

        with mock.patch.object(platformsupport, "process_is_alive", side_effect=_alive):
            with mock.patch.object(platformsupport, "process_start_token", side_effect=_token):
                with mock.patch.object(
                    platformsupport,
                    "kill_process_tree_by_pid",
                    side_effect=lambda pid: killed.append(pid),
                ):
                    attempted = peer_stop.kill_recorded_wrapper(doc)
        self.assertFalse(attempted)
        self.assertEqual(killed, [])

    def test_current_stop_owner_requires_non_empty_identity_token(self) -> None:
        """Empty/missing local startToken must fail closed before claim writes stopping."""
        for bad in ("", None):
            with self.subTest(token=bad):
                with mock.patch.object(
                    platformsupport, "process_start_token", return_value=bad
                ):
                    with self.assertRaises(GrokWrapperError) as ctx:
                        peer_stop._current_stop_owner()
                self.assertEqual(ctx.exception.error_class, "acp-failure")

    def test_claim_peer_stop_refuses_empty_identity_token_without_poisoning(self) -> None:
        """Claim must not write lifecycle=stopping when local identity token is empty."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "running"
        peer_doc.pop("stopOwner", None)
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        with mock.patch.object(platformsupport, "process_start_token", return_value=""):
            with self.assertRaises(GrokWrapperError) as ctx:
                peer_stop.claim_peer_stop(run_paths)
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        after = self._read_peer(run_paths)
        self.assertEqual(after.get("lifecycle"), "running")
        self.assertNotIn("stopOwner", after)

    def test_multi_stopper_undurable_failure_reclaim_returns_honest_failure_then_success(
        self,
    ) -> None:
        """Undurable finalize stays reclaimable; second stopper can reclaim after owner dies.

        First stopper returns honest failure (not undurable success) and leaves
        stopping+stopOwner. After the owner is dead past grace, a second claim
        finalizes with durable success.
        """
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        owner_pid = 555001
        owner_token = "first-stopper-token"
        peer_doc = dict(peer_doc)
        peer_doc["lifecycle"] = "stopping"
        peer_doc["stopOwner"] = {
            "pid": owner_pid,
            "startToken": owner_token,
            "claimedAt": time.time(),
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        undurable_success = {
            "schemaVersion": 1,
            "runId": run_paths.run_id,
            "mode": "peer-start",
            "status": "success",
            "response": {"peer": {"stopped": True}},
        }

        # First stopper (current owner path): finalize returns undurable success.
        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            return_value=dict(undurable_success),
        ):
            with mock.patch.object(
                peer_stop, "load_durable_terminal_envelope", return_value=None
            ):
                env1 = peer_stop._finalize_and_mark(
                    run_paths=run_paths,
                    peer_doc=peer_doc,
                    home_path=pathlib.Path(peer_doc["homePath"]),
                    worktree=wt,
                    contract=None,
                    original_baseline=baseline,
                    stage=stage,
                )
        self.assertEqual(env1.get("status"), "failure")
        self.assertEqual(env1.get("error", {}).get("class"), "state-ownership-violation")
        after1 = self._read_peer(run_paths)
        self.assertEqual(after1.get("lifecycle"), "stopping")
        self.assertEqual((after1.get("stopOwner") or {}).get("pid"), owner_pid)

        # Owner dies: second stopper reclaims and durable-finalizes successfully.
        durable = {
            "schemaVersion": 1,
            "runId": run_paths.run_id,
            "mode": "peer-start",
            "status": "success",
            "response": {"peer": {"stopped": True, "reclaimed": True}},
        }

        def _alive(pid):
            return False

        def _token(pid):
            if pid == os.getpid():
                return "second-stopper-token"
            return None

        with mock.patch.object(platformsupport, "process_is_alive", side_effect=_alive):
            with mock.patch.object(platformsupport, "process_start_token", side_effect=_token):
                with mock.patch.object(peer_stop, "_PEER_STOP_OWNER_GRACE_SECONDS", 0.0):
                    outcome, claimed, durable_existing = peer_stop.claim_peer_stop(run_paths)
        self.assertEqual(outcome, "claimed")
        self.assertIsNone(durable_existing)
        self.assertEqual(claimed.get("lifecycle"), "stopping")
        self.assertNotEqual((claimed.get("stopOwner") or {}).get("pid"), owner_pid)
        self.assertEqual(
            (claimed.get("stopOwner") or {}).get("startToken"), "second-stopper-token"
        )

        def _fake_finalize(**kwargs):
            return dict(durable)

        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session",
            side_effect=_fake_finalize,
        ):
            with mock.patch.object(
                peer_stop, "load_durable_terminal_envelope", return_value=durable
            ):
                env2 = peer_stop._finalize_and_mark(
                    run_paths=run_paths,
                    peer_doc=claimed,
                    home_path=pathlib.Path(peer_doc["homePath"]),
                    worktree=wt,
                    contract=None,
                    original_baseline=baseline,
                    stage=stage,
                )
        self.assertEqual(env2.get("status"), "success")
        self.assertEqual(env2.get("runId"), run_paths.run_id)

    def test_peer_prompt_fails_closed_for_terminal_and_stopping_lifecycles(self) -> None:
        """peer-prompt must refuse stopping/stopped/failed/died without relying on socket."""
        for life in ("stopping", "stopped", "failed", "died"):
            with self.subTest(lifecycle=life):
                run_paths = runstate.create_run("peer-start")
                sock = run_paths.run_dir / "peer.sock"
                # Socket path present so failure cannot be blamed on missing socket alone.
                sock.write_text("", encoding="utf-8")
                peer_doc = {
                    "schemaVersion": 1,
                    "lifecycle": life,
                    "sessionId": "s1",
                    "socketPath": str(sock),
                    "wrapper": {"pid": os.getpid(), "startToken": "w"},
                    "child": {
                        "pid": os.getpid(),
                        "startToken": platformsupport.process_start_token(os.getpid()) or "c",
                    },
                    "homePath": str(pathlib.Path(self.tmp_root) / "home"),
                    "worktreePath": str(self.repo),
                    "leaseExpiresAt": time.time() + 3600,
                }
                if life == "stopping":
                    peer_doc["stopOwner"] = {
                        "pid": 1,
                        "startToken": "x",
                        "claimedAt": time.time(),
                    }
                runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)
                ns = mock.Mock(run_id=run_paths.run_id, task="hi", task_file=None)
                with mock.patch.object(peer_mod, "_connect_control") as connect:
                    env = peer_mod.run_peer_prompt(ns)
                connect.assert_not_called()
                self.assertEqual(env["status"], "failure")
                self.assertEqual(env["mode"], "peer-prompt")
                self.assertEqual(env["runId"], run_paths.run_id)
                self.assertEqual(env["error"]["class"], "acp-failure")
                self.assertIn(life, json.dumps(env).lower())

    def test_resident_control_prompt_refuses_disk_stopping_lifecycle(self) -> None:
        """Resident control prompt re-reads disk lifecycle (defense-in-depth vs stop claim)."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        on_disk = dict(peer_doc)
        on_disk["lifecycle"] = "stopping"
        on_disk["stopOwner"] = {
            "pid": 42,
            "startToken": "stopper",
            "claimedAt": time.time(),
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", on_disk)

        stale = dict(peer_doc)
        stale["lifecycle"] = "running"
        stale.pop("stopOwner", None)
        session = peer_mod.PeerSession(
            run_id=run_paths.run_id,
            run_paths=run_paths,
            session_id="sess-1",
            socket_path=run_paths.run_dir / "peer.sock",
            acp=mock.Mock(session_prompt=mock.Mock(return_value={"stopReason": "end_turn"})),
            child=mock.Mock(poll=mock.Mock(return_value=None)),
            home=home,
            worktree=wt,
            progress=mock.Mock(),
            peer_doc=stale,
            contract=None,
            original_baseline=baseline,
            model="grok-4.5",
            sentinel_name=peer_doc["sentinelName"],
        )
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod._handle_prompt(session, "late prompt after stop claim")
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        self.assertIn("stopping", str(ctx.exception))
        session.acp.session_prompt.assert_not_called()
        self.assertEqual(session.peer_doc.get("lifecycle"), "stopping")


if __name__ == "__main__":
    unittest.main()
