# wrapper/scripts/tests/test_worktree_lock.py
#
# Repo-scoped lock around git worktree add/remove/prune. Concurrent worktree
# add on one repo races in git (.git/worktrees/*/commondir). These tests fail
# if the lock is missing or not held during the mutating git call.

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from groklib import runstate
from groklib import worktree as worktree_mod
from groklib.worktree_lock import repo_worktree_lock, repo_worktree_lock_path

from tests import gitfixtures


try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def _try_exclusive_nb(lock_path: pathlib.Path) -> bool:
    """Return True if LOCK_EX|LOCK_NB succeeded (lock was free)."""
    if fcntl is None:
        raise unittest.SkipTest("fcntl required")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class RepoWorktreeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-wt-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = gitfixtures.make_repo(self.tmp)

    def test_lock_path_is_inside_repo_git_dir(self) -> None:
        path = repo_worktree_lock_path(self.repo)
        self.assertEqual(path.parent, (self.repo / ".git").resolve())
        self.assertEqual(path.name, "grok-skills-worktree.lock")

    def test_same_repo_lock_serializes_threads(self) -> None:
        inside = 0
        max_inside = 0
        guard = threading.Lock()

        def _hold() -> None:
            nonlocal inside, max_inside
            with repo_worktree_lock(self.repo):
                with guard:
                    inside += 1
                    max_inside = max(max_inside, inside)
                time.sleep(0.08)
                with guard:
                    inside -= 1

        threads = [threading.Thread(target=_hold) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        self.assertEqual(max_inside, 1)

    def test_nested_lock_same_thread_does_not_deadlock(self) -> None:
        with repo_worktree_lock(self.repo):
            with repo_worktree_lock(self.repo):
                self.assertFalse(_try_exclusive_nb(repo_worktree_lock_path(self.repo)))

    def test_create_external_worktree_holds_lock_during_add(self) -> None:
        seen = {"held": False}
        real = worktree_mod._git

        def _git(repo, *args, **kwargs):
            if args[:2] == ("worktree", "add"):
                seen["held"] = not _try_exclusive_nb(repo_worktree_lock_path(repo))
            return real(repo, *args, **kwargs)

        with mock.patch.object(worktree_mod, "_git", _git):
            rid = runstate.new_run_id()
            env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state")}
            with mock.patch.dict(os.environ, env):
                wt = worktree_mod.create_external_worktree(
                    repo_root=self.repo, base="HEAD", run_id=rid
                )
        self.assertTrue(seen["held"], "worktree add must run while the repo lock is held")
        worktree_mod.remove_external_worktree(wt, confirmed=True, expected_run_id=rid)

    def test_run_lock_uses_shared_exclusive_file_lock(self) -> None:
        import inspect

        from groklib.filelock import exclusive_file_lock

        source = inspect.getsource(runstate.run_lock)
        self.assertIn("exclusive_file_lock", source)
        self.assertIs(runstate.exclusive_file_lock, exclusive_file_lock)
