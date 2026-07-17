# wrapper/scripts/tests/test_lifecycle_failclosed.py
"""Fail-closed when durable run-record CAS cannot advance to running."""

import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from groklib import GrokWrapperError, runstate
from groklib.authhome import PrivateHome
from groklib.grokcli import GrokRunResult
from groklib.modes import _shared
from groklib.modes._envelope import ModeRun
from groklib.modes._shared import _run_grok_mode_body
from groklib.progress import ProgressWriter


def _make_mode_run(**overrides) -> ModeRun:
    fields = dict(
        mode="review",
        binary=Path("/nonexistent/grok"),
        requested_model="grok-4.5",
        web_access=False,
        output_schema=None,
        timeout_seconds=30,
        max_turns=None,
        prompt_text="x",
        cwd=Path("."),
        tools=("read_file",),
        instructions=[],
        repository=None,
        target_workspace=None,
        detect_unexpected_edits=False,
    )
    fields.update(overrides)
    return ModeRun(**fields)


class LifecycleCasFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cas-fail-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_run_grok_body_raises_after_cas_retry_fails(self) -> None:
        paths = runstate.create_run("review")
        progress = ProgressWriter(paths.run_id, paths.progress_path)
        run = _make_mode_run(cwd=Path(self.tmp_root))
        with mock.patch.object(
            runstate, "cas_update_run_record", side_effect=OSError("state root unwritable")
        ):
            with mock.patch.object(
                runstate, "set_lifecycle", side_effect=OSError("state root unwritable")
            ):
                with self.assertRaises(GrokWrapperError) as ctx:
                    _run_grok_mode_body(run, paths, progress, [None], [None])
        self.assertEqual(ctx.exception.error_class, "state-ownership-violation")
        self.assertIn("run-record-cas-failed", str(ctx.exception.detail.get("reason", "")))
        # Must not have progressed to a Grok spawn (no private home lifecycle)
        self.assertEqual(runstate.load_run_record(paths.run_id)["lifecycle"], "created")

    def test_initial_warnings_seed_envelope_warnings(self) -> None:
        """ModeRun.initial_warnings ride the success envelope warnings list."""
        paths = runstate.create_run("review")
        progress = ProgressWriter(paths.run_id, paths.progress_path)
        run = _make_mode_run(cwd=Path(self.tmp_root), initial_warnings=("seeded",))

        private_home = PrivateHome(
            home_dir=Path(self.tmp_root) / "fake-home",
            grok_dir=Path(self.tmp_root) / "fake-home" / ".grok",
            config_path=Path(self.tmp_root) / "fake-home" / ".grok" / "config.toml",
        )
        private_home.home_dir.mkdir(parents=True)
        private_home.grok_dir.mkdir(parents=True)

        fake_result = GrokRunResult(
            argv=("/nonexistent/grok",),
            exit_status=0,
            stdout="{}",
            stderr="",
            duration_seconds=0.01,
            parsed={"usage": {}, "num_turns": 1},
            stop_reason="end_turn",
            session_id="sess",
            request_id="req",
            model_usage=None,
            effective_model="grok-4.5",
            final_text="ok",
            structured=None,
        )
        sandbox_obj = {
            "requestedProfile": "read-only",
            "reportedProfile": "read-only",
            "enforced": True,
            "evidence": "test",
        }

        def _fake_execute(*args, **_kwargs):
            # Mirror real _execute_and_verify: publish into the result holder so
            # the success path sees a completed Grok answer.
            if len(args) >= 7 and args[6] is not None:
                args[6][0] = fake_result
            return fake_result, sandbox_obj, "grok-4.5"

        with mock.patch.object(_shared, "create_private_home", return_value=private_home), mock.patch(
            "groklib.preflight_cache.ensure_ready", return_value=None
        ), mock.patch(
            "groklib.platformsupport.require_probed_platform_for_live", return_value=None
        ), mock.patch.object(
            _shared, "_execute_and_verify", side_effect=_fake_execute
        ), mock.patch.object(
            _shared, "policy_for_mode", return_value=types.SimpleNamespace()
        ), mock.patch.object(
            _shared, "render_sandbox_toml", return_value=""
        ), mock.patch.object(
            _shared, "render_config_toml", return_value=""
        ), mock.patch(
            "groklib.modes._envelope.destroy_private_home",
            return_value={"status": "clean", "detail": None},
        ), mock.patch.object(
            _shared, "_capture_review_fs_baseline", return_value=None
        ), mock.patch.object(
            _shared, "_report_repo_fs_drift", return_value=None
        ):
            envelope = _run_grok_mode_body(run, paths, progress, [None], [None])

        self.assertEqual(envelope["status"], "success", envelope)
        self.assertIn("seeded", envelope.get("warnings") or [])


if __name__ == "__main__":
    unittest.main()
