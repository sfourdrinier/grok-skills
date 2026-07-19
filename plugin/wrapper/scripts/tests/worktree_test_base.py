# wrapper/scripts/tests/worktree_test_base.py

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from groklib import runstate
from groklib.worktree import ExternalWorktree, create_external_worktree

from tests import gitfixtures


def _git(repo: pathlib.Path, *args: str) -> str:
    argv = ["git", "-C", str(repo)] + [str(arg) for arg in args]
    completed = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        raise AssertionError(
            "test git helper failed: {} exit {} stderr={!r}".format(args, completed.returncode, completed.stderr)
        )
    return completed.stdout

class WorktreeTestBase(unittest.TestCase):
    """Isolates XDG_STATE_HOME (worktree root) and the fixture repo under tempfile."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-worktree-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        repo_parent = tempfile.mkdtemp(prefix="grok-cli-repo-", dir=self.tmp_root)
        self.repo_root = gitfixtures.make_repo(repo_parent)
        self.base = gitfixtures.head_revision(self.repo_root)

    def _create(self) -> ExternalWorktree:
        run_id = runstate.new_run_id()
        return create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)

    def _force_remove(self, wt: ExternalWorktree) -> None:
        # Test-only teardown that force-removes regardless of dirty state so the
        # temp repo's worktree registry is left clean; the tmp_root rmtree then
        # deletes everything on disk. Production removal (remove_external_worktree)
        # deliberately refuses dirty worktrees, so it cannot serve as teardown.
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(wt.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_root), "branch", "-D", wt.branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        if marker.exists():
            marker.unlink()

    def _worktree_list_paths(self) -> list:
        out = _git(self.repo_root, "worktree", "list", "--porcelain")
        paths = []
        for line in out.splitlines():
            if line.startswith("worktree "):
                paths.append(pathlib.Path(line[len("worktree "):]).resolve())
        return paths
