# wrapper/scripts/tests/test_git_timeout.py
import os
import unittest
from unittest import mock

from groklib import git_timeout, worktree


class GitTimeoutTests(unittest.TestCase):
    def test_default_is_generous(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROK_WRAPPER_GIT_TIMEOUT_SECONDS", None)
            self.assertEqual(git_timeout.git_timeout_seconds(), 600)
            self.assertEqual(worktree.git_timeout_seconds(), 600)

    def test_env_override_clamped(self) -> None:
        with mock.patch.dict(os.environ, {"GROK_WRAPPER_GIT_TIMEOUT_SECONDS": "120"}):
            self.assertEqual(git_timeout.git_timeout_seconds(), 120)
        with mock.patch.dict(os.environ, {"GROK_WRAPPER_GIT_TIMEOUT_SECONDS": "5"}):
            self.assertEqual(git_timeout.git_timeout_seconds(), 30)
        with mock.patch.dict(os.environ, {"GROK_WRAPPER_GIT_TIMEOUT_SECONDS": "999999"}):
            self.assertEqual(git_timeout.git_timeout_seconds(), 7200)
        with mock.patch.dict(os.environ, {"GROK_WRAPPER_GIT_TIMEOUT_SECONDS": "nope"}):
            self.assertEqual(git_timeout.git_timeout_seconds(), 600)
