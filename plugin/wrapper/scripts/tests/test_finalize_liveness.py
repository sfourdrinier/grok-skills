# wrapper/scripts/tests/test_finalize_liveness.py
# Liveness marker / fail-closed probe tests (split from test_finalize_watchdog for 900-line cap).

import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from groklib.modes import finalize_worker


class FinalizeLivenessFailClosedTests(unittest.TestCase):
    """C2: unknown marker state must not fail open as dead."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-cli-liveness-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = pathlib.Path(self.tmp) / "run"
        self.run_dir.mkdir()

    def test_liveness_unreadable_marker_is_unknown(self) -> None:
        from groklib.modes import finalize_worker as fw
        marker = self.run_dir / "finalize-worker.pid"
        marker.write_text("not-json-or-int\n", encoding="utf-8")
        # Make unreadable by replacing read with OSError via chmod 0 when possible
        self.assertEqual(fw.finalize_worker_liveness(self.run_dir), "unknown")
        self.assertTrue(fw.finalize_worker_blocks_durable_write(self.run_dir))
        self.assertFalse(fw.finalize_worker_is_alive(self.run_dir))

    def test_liveness_missing_marker_is_dead(self) -> None:
        from groklib.modes import finalize_worker as fw
        self.assertEqual(fw.finalize_worker_liveness(self.run_dir), "dead")
        self.assertFalse(fw.finalize_worker_blocks_durable_write(self.run_dir))

    def test_starting_marker_is_alive(self) -> None:
        from groklib.modes import finalize_worker as fw
        fw.mark_worker_starting(self.run_dir)
        self.assertEqual(fw.finalize_worker_liveness(self.run_dir), "alive")
        self.assertTrue(fw.finalize_worker_blocks_durable_write(self.run_dir))

    def test_worker_main_calls_setsid_when_available(self) -> None:
        from groklib.modes import finalize_worker as fw
        called = []
        real = getattr(os, "setsid", None)
        def spy():
            called.append(True)
            if real is not None:
                try:
                    real()
                except OSError:
                    pass
        with mock.patch.object(os, "setsid", spy, create=True):
            # Invoke only the detach helper
            fw._detach_worker_process_group()
        self.assertTrue(called)

    def test_starting_marker_dead_parent_is_dead(self) -> None:
        """SIGKILL after starting marker: parent gone => not forever-alive."""
        from groklib.modes import finalize_worker as fw

        path = self.run_dir / "finalize-worker.pid"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "state": "starting",
                    "parentPid": 999999999,
                    "parentStartToken": "not-a-real-token",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(fw.finalize_worker_liveness(self.run_dir), "dead")
        self.assertFalse(fw.finalize_worker_blocks_durable_write(self.run_dir))
if __name__ == "__main__":
    unittest.main()
