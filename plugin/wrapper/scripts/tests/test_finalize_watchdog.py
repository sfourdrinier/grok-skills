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

    def test_payload_is_json_serializable(self) -> None:
        """Design §9: finalize-payload.json must be pure JSON (no live objects)."""
        paths, env, rev = self._prepare_finalizing()
        finalize_worker.run_finalize_parent(
            paths,
            mode="review",
            envelope=env,
            lifecycle="completed",
            expected_revision=rev,
            budget_seconds=60,
        )
        payload_path = paths.run_dir / "finalize-payload.json"
        self.assertTrue(payload_path.is_file())
        raw = payload_path.read_text(encoding="utf-8")
        loaded = json.loads(raw)
        # Round-trip dump must succeed (no non-JSON types survived).
        again = json.dumps(loaded)
        self.assertIsInstance(again, str)
        self.assertEqual(loaded["schemaVersion"], 1)
        self.assertEqual(loaded["runId"], paths.run_id)
        self.assertIsInstance(loaded["envelope"], dict)
        self.assertNotIn("Process", again)
        self.assertNotIn("function", again)

    def test_parent_recovery_only_authors_authorized_failure_classes(self) -> None:
        """Parent-synthesized new envelopes use only authorized recovery classes."""
        authorized = {
            "finalization-timeout",
            "cli-failure",
            "finalization-worker-missing-result",
            "finalization-worker-unkillable",
        }

        cases = [
            # (proc factory state, expected class)
            ("missing", "finalization-worker-missing-result"),
            ("timeout", "finalization-timeout"),
            ("cli", "cli-failure"),
            ("unkillable", "finalization-worker-unkillable"),
        ]

        for case, expected in cases:
            with self.subTest(case=case):
                paths, env, rev = self._prepare_finalizing()
                state = {"alive_checks": 0}

                class FakeProc:
                    def __init__(self):
                        self.exitcode = 0 if case == "missing" else (7 if case == "cli" else None)

                    def start(self):
                        return None

                    def join(self, timeout=None):
                        return None

                    def is_alive(self):
                        if case == "unkillable":
                            return True
                        if case == "timeout":
                            state["alive_checks"] += 1
                            return state["alive_checks"] <= 2
                        return False

                    def terminate(self):
                        return None

                    def kill(self):
                        if case == "timeout":
                            self.exitcode = -9
                        return None

                class FakeCtx:
                    def Process(self, **kwargs):
                        return FakeProc()

                with mock.patch.object(
                    finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()
                ):
                    out, ephemeral, durable = finalize_worker.run_finalize_parent(
                        paths,
                        mode="review",
                        envelope=env,
                        lifecycle="completed",
                        expected_revision=rev,
                        budget_seconds=1,
                    )
                # When parent authors a new envelope (no pre-existing success body),
                # its error class must be in the authorized recovery set.
                if out.get("status") == "failure":
                    self.assertIn(out["error"]["class"], authorized)
                    self.assertEqual(out["error"]["class"], expected)
                if case == "unkillable":
                    self.assertEqual(ephemeral, "finalization-worker-unkillable")
                    self.assertFalse(durable)

    def test_worker_envelope_during_kill_window_is_returned(self) -> None:
        """If the worker writes envelope.json on kill, parent returns that body."""
        paths, env, rev = self._prepare_finalizing()
        worker_env = build_envelope(
            run_id=paths.run_id,
            mode="review",
            status="success",
            response={"answer": "from-worker-during-kill"},
        )
        state = {"alive_checks": 0}

        class FakeProc:
            def __init__(self):
                self.exitcode = None

            def start(self):
                return None

            def join(self, timeout=None):
                return None

            def is_alive(self):
                state["alive_checks"] += 1
                # First checks (budget + post-terminate): alive; after kill: dead
                return state["alive_checks"] <= 2

            def terminate(self):
                return None

            def kill(self):
                # Materialize envelope between kill and the next is_alive False
                runstate.persist_terminal_envelope(paths, rev, worker_env, lifecycle="completed")
                self.exitcode = -9
                return None

        class FakeCtx:
            def Process(self, **kwargs):
                return FakeProc()

        with mock.patch.object(
            finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()
        ):
            out, ephemeral, durable = finalize_worker.run_finalize_parent(
                paths,
                mode="review",
                envelope=env,
                lifecycle="completed",
                expected_revision=rev,
                budget_seconds=1,
            )
        self.assertIsNone(ephemeral)
        self.assertTrue(durable)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["response"]["answer"], "from-worker-during-kill")
        # Must not replace with timeout synthetic
        self.assertNotEqual((out.get("error") or {}).get("class"), "finalization-timeout")

    def test_progress_finalizing_before_done_on_success(self) -> None:
        """_publish_terminal_envelope: finalizing events precede done; no pre-done."""
        from groklib.modes._shared import _publish_terminal_envelope
        from groklib.progress import ProgressWriter, read_events

        paths, env, rev = self._prepare_finalizing()
        # Back lifecycle to running so _publish advances to finalizing
        rec = runstate.load_run_record(paths.run_id)
        # re-create path: leave as finalizing is fine; emit events through parent
        progress = ProgressWriter(paths.run_id, paths.progress_path)
        progress.emit("start", "run created")
        warnings = []
        published = _publish_terminal_envelope(
            paths,
            "review",
            env,
            lifecycle="completed",
            progress=progress,
            warnings=warnings,
        )
        self.assertEqual(published["status"], "success")
        events, _warnings = read_events(paths.progress_path)
        phases = [e.get("phase") for e in events]
        self.assertIn("finalizing", phases)
        self.assertIn("done", phases)
        # First finalizing must precede first done
        self.assertLess(phases.index("finalizing"), phases.index("done"))
        # Durable success must end with a completed done message
        self.assertTrue(
            any(
                e.get("phase") == "done" and "completed" in (e.get("message") or "")
                for e in events
            ),
            events,
        )

    def test_forced_finalize_failure_no_run_completed_done(self) -> None:
        """Unkillable finalize must not leave a 'run completed' done event."""
        from groklib.modes._shared import _publish_terminal_envelope
        from groklib.progress import ProgressWriter, read_events

        paths, env, rev = self._prepare_finalizing()
        progress = ProgressWriter(paths.run_id, paths.progress_path)
        progress.emit("start", "run created")

        class FakeProc:
            def __init__(self):
                self.exitcode = None
                self._alive = True

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

        with mock.patch.object(
            finalize_worker.multiprocessing, "get_context", return_value=FakeCtx()
        ):
            published = _publish_terminal_envelope(
                paths,
                "review",
                env,
                lifecycle="completed",
                progress=progress,
                warnings=[],
            )
        self.assertTrue(published.get("doNotStore"))
        events, _warnings = read_events(paths.progress_path)
        done_msgs = [
            e.get("message") or ""
            for e in events
            if e.get("phase") == "done"
        ]
        self.assertFalse(
            any("run completed" in m for m in done_msgs),
            "premature done event: {}".format(events),
        )

