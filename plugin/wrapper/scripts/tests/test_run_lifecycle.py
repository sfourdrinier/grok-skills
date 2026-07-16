# wrapper/scripts/tests/test_run_lifecycle.py
#
# Lifecycle seed/CAS/persist/effective_lifecycle tests (split from test_runstate.py
# to stay under the 900-line file cap).

import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from groklib import runstate

class CreateRunSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-seed-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def test_seed_lifecycle_created_status_running_revision_zero(self) -> None:
        paths = runstate.create_run("review")
        record = json.loads((paths.run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["lifecycle"], "created")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["recordRevision"], 0)
        self.assertEqual(record["runId"], paths.run_id)
        self.assertEqual(record["mode"], "review")

    def test_seed_exists_before_run_id_marker(self) -> None:
        order = []
        real_write = runstate.write_json_atomic
        real_emit = runstate.emit_run_id_marker

        def tracking_write(path, payload):
            if path.name == "run.json":
                order.append("seed")
            return real_write(path, payload)

        def tracking_emit(run_id):
            order.append("marker")
            return real_emit(run_id)

        with mock.patch.object(runstate, "write_json_atomic", side_effect=tracking_write):
            with mock.patch.object(runstate, "emit_run_id_marker", side_effect=tracking_emit):
                runstate.create_run("code")
        self.assertIn("seed", order)
        self.assertIn("marker", order)
        self.assertLess(order.index("seed"), order.index("marker"))

    def test_write_json_atomic_no_tmp_left(self) -> None:
        path = pathlib.Path(self.tmp_root) / "x.json"
        runstate.write_json_atomic(path, {"a": 1})
        runstate.write_json_atomic(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text())["a"], 2)
        self.assertEqual(list(path.parent.glob("x.json.tmp.*")), [])


    def test_persist_recovery_cancelled_envelope_is_canceled(self) -> None:
        """Envelope-first cancel crash recovery finishes lifecycle as canceled."""
        from groklib.envelope import failure_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = failure_envelope(
            run_id=paths.run_id,
            mode="review",
            error_class="cancelled",
            message="operator cancel",
        )
        runstate.write_json_atomic(paths.envelope_path, env)
        self.assertEqual(runstate.load_run_record(paths.run_id)["lifecycle"], "finalizing")
        finished = runstate.persist_terminal_envelope(paths, None, None)
        self.assertEqual(finished["lifecycle"], "canceled")
        self.assertEqual(finished["status"], "failure")


    def test_persist_cancelled_envelope_coerces_failed_to_canceled(self) -> None:
        from groklib.envelope import failure_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = failure_envelope(
            run_id=paths.run_id, mode="review", error_class="cancelled", message="bye"
        )
        rec = runstate.persist_terminal_envelope(paths, 2, env, lifecycle="failed")
        self.assertEqual(rec["lifecycle"], "canceled")


    def test_write_json_atomic_fsyncs_parent_directory(self) -> None:
        """After os.replace, parent dir is fsync'd for power-loss durability."""
        path = pathlib.Path(self.tmp_root) / "durable.json"
        fsynced_fds = []
        real_fsync = os.fsync
        real_open = os.open

        def tracking_fsync(fd):
            fsynced_fds.append(fd)
            return real_fsync(fd)

        dir_fds = []

        def tracking_open(path_str, flags, *args, **kwargs):
            fd = real_open(path_str, flags, *args, **kwargs)
            # O_RDONLY open of parent (no O_WRONLY) is the dir fsync path
            if flags == os.O_RDONLY and path_str == str(path.parent):
                dir_fds.append(fd)
            return fd

        with mock.patch.object(os, "fsync", side_effect=tracking_fsync):
            with mock.patch.object(os, "open", side_effect=tracking_open):
                runstate.write_json_atomic(path, {"ok": True})
        self.assertTrue(dir_fds, "expected O_RDONLY open of parent directory")
        self.assertTrue(
            any(fd in fsynced_fds for fd in dir_fds),
            "expected fsync on parent directory fd after replace",
        )
        self.assertEqual(json.loads(path.read_text())["ok"], True)

    def test_cas_update_and_conflict(self) -> None:
        paths = runstate.create_run("review")
        updated = runstate.cas_update_run_record(paths, 0, {"repository": "/repo"})
        self.assertEqual(updated["recordRevision"], 1)
        self.assertEqual(updated["repository"], "/repo")
        self.assertEqual(updated["lifecycle"], "created")
        with self.assertRaises(runstate.CasConflictError):
            runstate.cas_update_run_record(paths, 0, {"repository": "/other"})

    def test_set_lifecycle_graph_and_terminal_refuse(self) -> None:
        paths = runstate.create_run("review")
        r = runstate.set_lifecycle(paths, 0, "running")
        self.assertEqual(r["lifecycle"], "running")
        r = runstate.set_lifecycle(paths, 1, "finalizing")
        self.assertEqual(r["lifecycle"], "finalizing")
        with self.assertRaises(runstate.LifecycleError):
            runstate.set_lifecycle(paths, 2, "completed")
        from groklib.envelope import build_envelope

        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        runstate.persist_terminal_envelope(paths, 2, env, lifecycle="completed")
        with self.assertRaises(runstate.LifecycleError):
            runstate.set_lifecycle(paths, 3, "running")

    def test_persist_terminal_envelope_first_and_idempotent(self) -> None:
        from groklib.envelope import build_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        runstate.persist_terminal_envelope(paths, 2, env, lifecycle="completed")
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "completed")
        self.assertTrue(paths.envelope_path.is_file())
        body = paths.envelope_path.read_bytes()
        # Second call with different envelope must not replace
        env2 = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": False})
        runstate.persist_terminal_envelope(paths, None, env2, lifecycle="completed")
        self.assertEqual(paths.envelope_path.read_bytes(), body)

    def test_persist_crash_recovery_finishes_lifecycle(self) -> None:
        from groklib.envelope import build_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        # Simulate crash after envelope write before lifecycle
        runstate.write_json_atomic(paths.envelope_path, env)
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "finalizing")
        runstate.persist_terminal_envelope(paths, None, None)
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "completed")


    def test_public_write_run_record_removed(self) -> None:
        self.assertFalse(hasattr(runstate, "write_run_record"))

    def test_cas_cannot_set_success_status_while_nonterminal(self) -> None:
        paths = runstate.create_run("review")
        updated = runstate.cas_update_run_record(
            paths, 0, {"requestedModel": "grok-4.5", "status": "success"}
        )
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["lifecycle"], "created")
        self.assertFalse(paths.envelope_path.is_file())

    def test_concurrent_cas_conflict(self) -> None:
        import threading
        paths = runstate.create_run("review")
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                runstate.cas_update_run_record(paths, 0, {"repository": "/r"})
                results.append("ok")
            except runstate.CasConflictError:
                results.append("conflict")

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(runstate.load_run_record(paths.run_id)["recordRevision"], 1)

    def test_effective_lifecycle_resolution_order(self) -> None:
        # Terminal record without a coherent envelope is interrupted (H7).
        rec = {"lifecycle": "completed", "status": "success"}
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=False, envelope_status=None, process_liveness="dead"
        )
        self.assertEqual(life, "interrupted")
        self.assertEqual(src, "derived")
        # Terminal record + conflicting envelope status is interrupted.
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=True, envelope_status="failure", process_liveness="dead"
        )
        self.assertEqual(life, "interrupted")
        self.assertEqual(src, "derived")
        # Compatible terminal pair stays on the record.
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=True, envelope_status="success", process_liveness="dead"
        )
        self.assertEqual(life, "completed")
        self.assertEqual(src, "record")
        rec = {"lifecycle": "finalizing", "status": "running"}
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=True, envelope_status="success", process_liveness="dead"
        )
        self.assertEqual(life, "completed")
        self.assertEqual(src, "envelope")
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=False, envelope_status=None, process_liveness="dead"
        )
        self.assertEqual(life, "interrupted")
        self.assertEqual(src, "derived")

    def test_persist_requires_revision_and_matching_lifecycle(self) -> None:
        from groklib.envelope import build_envelope, failure_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, None, env, lifecycle="completed")
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, 2, env, lifecycle=None)
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, 2, env, lifecycle="failed")
        paths2 = runstate.create_run("code")
        fail = failure_envelope(
            run_id=paths2.run_id, mode="code", error_class="cli-failure", message="x"
        )
        runstate.set_lifecycle(paths2, 0, "running")
        runstate.persist_terminal_envelope(paths2, 1, fail, lifecycle="failed")
        self.assertEqual(runstate.load_run_record(paths2.run_id)["lifecycle"], "failed")

    def test_completed_from_running_refused(self) -> None:
        from groklib.envelope import build_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, 1, env, lifecycle="completed")

    def test_cas_rejects_unknown_keys(self) -> None:
        paths = runstate.create_run("review")
        with self.assertRaises(runstate.LifecycleError):
            runstate.cas_update_run_record(paths, 0, {"notAField": 1})


if __name__ == "__main__":
    unittest.main()
