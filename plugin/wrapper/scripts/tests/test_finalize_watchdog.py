# wrapper/scripts/tests/test_finalize_watchdog.py

import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from groklib import runstate
from groklib.envelope import build_envelope, failure_envelope
from groklib.modes import finalize_worker


class FinalizeWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-finalize-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _prepare_finalizing(self):
        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        rec = runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(
            run_id=paths.run_id, mode="review", status="success", response={"answer": "ok"}
        )
        return paths, env, int(rec["recordRevision"])

    def test_worker_persists_terminal_envelope(self) -> None:
        paths, env, rev = self._prepare_finalizing()
        out, ephemeral = finalize_worker.run_finalize_parent(
            paths,
            mode="review",
            envelope=env,
            lifecycle="completed",
            expected_revision=rev,
            budget_seconds=60,
        )
        self.assertIsNone(ephemeral)
        self.assertEqual(out["status"], "success")
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "completed")
        self.assertTrue(paths.envelope_path.is_file())

    def test_parent_refuses_durable_write_while_alive(self) -> None:
        paths, env, rev = self._prepare_finalizing()

        class FakeProc:
            def __init__(self):
                self._alive = True
                self.exitcode = None

            def start(self):
                return None

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return self._alive

            def terminate(self):
                return None

            def kill(self):
                return None

        class FakeCtx:
            def Process(self, **kwargs):
                return FakeProc()

        with mock.patch.object(finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()):
            out, ephemeral = finalize_worker.run_finalize_parent(
                paths,
                mode="review",
                envelope=env,
                lifecycle="completed",
                expected_revision=rev,
                budget_seconds=1,
            )
        self.assertEqual(ephemeral, "finalization-worker-unkillable")
        self.assertEqual(out["error"]["class"], "finalization-worker-unkillable")
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "finalizing")
        self.assertFalse(paths.envelope_path.is_file())

    def test_terminal_envelope_never_replaced(self) -> None:
        paths, env, rev = self._prepare_finalizing()
        finalize_worker.run_finalize_parent(
            paths,
            mode="review",
            envelope=env,
            lifecycle="completed",
            expected_revision=rev,
            budget_seconds=60,
        )
        body = paths.envelope_path.read_bytes()
        other = failure_envelope(
            run_id=paths.run_id,
            mode="review",
            error_class="finalization-timeout",
            message="should not replace",
        )
        # Recovery path with existing envelope must preserve body
        runstate.persist_terminal_envelope(paths, None, other, lifecycle="failed")
        self.assertEqual(paths.envelope_path.read_bytes(), body)
