# wrapper/scripts/tests/test_lifecycle_failclosed.py
"""Fail-closed when durable run-record CAS cannot advance to running."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from groklib import GrokWrapperError, runstate
from groklib.modes._envelope import ModeRun
from groklib.modes._shared import _run_grok_mode_body
from groklib.progress import ProgressWriter


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
        run = ModeRun(
            mode="review",
            binary=Path("/nonexistent/grok"),
            requested_model="grok-4.5",
            web_access=False,
            output_schema=None,
            timeout_seconds=30,
            max_turns=None,
            prompt_text="x",
            cwd=Path(self.tmp_root),
            tools=("read_file",),
            instructions=[],
            repository=None,
            target_workspace=None,
            detect_unexpected_edits=False,
        )
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


if __name__ == "__main__":
    unittest.main()
