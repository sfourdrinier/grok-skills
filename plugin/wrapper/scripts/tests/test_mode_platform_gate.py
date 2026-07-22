# wrapper/scripts/tests/test_mode_platform_gate.py
#
# Mode-level SEC1 / Linux bwrap gates for worktree code path. Kept separate so
# test_mode_code.py stays under the 900-line cap (AGENTS rule 11). Review and
# preflight own their sibling cases in their own modules.

import json
from unittest import mock

from groklib import platformsupport

from tests.worktreefixtures import WorktreeModeHarness


def _plant_sentinel_in_worktree(worktree_path, run_id: str) -> None:
    (worktree_path / (".grok-run-" + run_id)).write_text("", encoding="utf-8")


class CodePlatformGateTests(WorktreeModeHarness):
    """code: unprobed OS / missing bwrap fail closed before worktree or spawn."""

    def _run(self, repo, **kwargs):
        return self.drive(
            ["code", "--target", "pkg", "--base", "HEAD", "--task", "Fix the module"],
            repo_root=repo,
            **kwargs,
        )

    def test_code_linux_without_bwrap_blocks_before_worktree_and_spawn(self) -> None:
        repo = self.make_code_repo()
        worktrees_before = self._worktree_dirs()
        homes_before = self.temp_home_prefix_dirs()
        with mock.patch.object(platformsupport, "current_platform", lambda: "linux"):
            with mock.patch("shutil.which", return_value=None):
                exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "probe-required")
        self.assertIn("bwrap", env["error"]["message"].lower())
        self.assertEqual(self._worktree_dirs() - worktrees_before, set())
        self.assertEqual(self.temp_home_prefix_dirs() - homes_before, set())
        self.assertFalse(self.argv_log_path.exists(), "grok must not spawn without bwrap on Linux")
