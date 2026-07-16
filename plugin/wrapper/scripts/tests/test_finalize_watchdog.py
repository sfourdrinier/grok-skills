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
        out, ephemeral, durable = finalize_worker.run_finalize_parent(
            paths,
            mode="review",
            envelope=env,
            lifecycle="completed",
            expected_revision=rev,
            budget_seconds=60,
        )
        self.assertIsNone(ephemeral)
        self.assertTrue(durable)
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
            out, ephemeral, durable = finalize_worker.run_finalize_parent(
                paths,
                mode="review",
                envelope=env,
                lifecycle="completed",
                expected_revision=rev,
                budget_seconds=1,
            )
        self.assertEqual(ephemeral, "finalization-worker-unkillable")
        self.assertFalse(durable)
        self.assertTrue(out.get("doNotStore"))
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

    def test_exit_0_without_envelope_is_missing_result(self) -> None:
        paths, env, rev = self._prepare_finalizing()

        class FakeProc:
            def __init__(self):
                self.exitcode = 0

            def start(self):
                return None

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

            def terminate(self):
                return None

            def kill(self):
                return None

        class FakeCtx:
            def Process(self, **kwargs):
                return FakeProc()

        with mock.patch.object(finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()):
            out, ephemeral, durable = finalize_worker.run_finalize_parent(
                paths,
                mode="review",
                envelope=env,
                lifecycle="completed",
                expected_revision=rev,
                budget_seconds=5,
            )
        self.assertIsNone(ephemeral)
        self.assertEqual(out["error"]["class"], "finalization-worker-missing-result")
        self.assertEqual(runstate.load_run_record(paths.run_id)["lifecycle"], "failed")

    def test_existing_envelope_not_replaced_by_timeout_stdout(self) -> None:
        paths, env, rev = self._prepare_finalizing()
        runstate.persist_terminal_envelope(paths, rev, env, lifecycle="completed")
        body = paths.envelope_path.read_bytes()

        class FakeProc:
            def __init__(self):
                self.exitcode = None

            def start(self):
                return None

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

            def terminate(self):
                return None

            def kill(self):
                return None

        class FakeCtx:
            def Process(self, **kwargs):
                return FakeProc()

        with mock.patch.object(finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()):
            out, ephemeral, durable = finalize_worker.run_finalize_parent(
                paths,
                mode="review",
                envelope=env,
                lifecycle="completed",
                expected_revision=rev + 10,
                budget_seconds=1,
            )
        self.assertTrue(durable)
        self.assertEqual(out["status"], "success")
        self.assertEqual(paths.envelope_path.read_bytes(), body)

    def test_nonzero_exit_cli_failure(self) -> None:
        paths, env, rev = self._prepare_finalizing()

        class FakeProc:
            def __init__(self):
                self.exitcode = 7
            def start(self):
                return None
            def join(self, timeout=None):
                return None
            def is_alive(self):
                return False
            def terminate(self):
                return None
            def kill(self):
                return None

        class FakeCtx:
            def Process(self, **kwargs):
                return FakeProc()

        with mock.patch.object(finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()):
            out, ephemeral, durable = finalize_worker.run_finalize_parent(
                paths, mode="review", envelope=env, lifecycle="completed",
                expected_revision=rev, budget_seconds=5,
            )
        self.assertIsNone(ephemeral)
        self.assertEqual(out["error"]["class"], "cli-failure")
        self.assertEqual(runstate.load_run_record(paths.run_id)["lifecycle"], "failed")

    def test_timeout_path_durable_failure(self) -> None:
        paths, env, rev = self._prepare_finalizing()
        # Worker appears alive then dead after kill; timed_out True, no envelope
        state = {"alive_checks": 0}

        class FakeProc:
            def __init__(self):
                self.exitcode = None
            def start(self):
                return None
            def join(self, timeout=None):
                return None
            def is_alive(self):
                # After budget join and after terminate: alive; after kill: dead
                state["alive_checks"] += 1
                return state["alive_checks"] <= 2
            def terminate(self):
                return None
            def kill(self):
                self.exitcode = -9
                return None

        class FakeCtx:
            def Process(self, **kwargs):
                return FakeProc()

        with mock.patch.object(finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()):
            out, ephemeral, durable = finalize_worker.run_finalize_parent(
                paths, mode="review", envelope=env, lifecycle="completed",
                expected_revision=rev, budget_seconds=1,
            )
        self.assertIsNone(ephemeral)
        self.assertEqual(out["error"]["class"], "finalization-timeout")
        self.assertEqual(runstate.load_run_record(paths.run_id)["lifecycle"], "failed")
        self.assertTrue(paths.envelope_path.is_file())

    def test_stderr_private_permissions(self) -> None:
        import stat as statmod
        paths, env, rev = self._prepare_finalizing()
        # Force worker path that writes stderr by calling worker main with bad persist
        # Simpler: call _write_private_text
        err = paths.run_dir / "finalize-worker.stderr"
        finalize_worker._write_private_text(err, "boom")
        mode = statmod.S_IMODE(err.stat().st_mode)
        self.assertEqual(mode, 0o600)

