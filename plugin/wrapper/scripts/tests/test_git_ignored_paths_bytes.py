# wrapper/scripts/tests/test_git_ignored_paths_bytes.py
#
# Bytes/surrogateescape safety for direct_finalize.git_ignored_paths
# (path_inventory / worktree._run_git_bytes SSOT).

import os
import pathlib
import subprocess
import tempfile
import unittest

class GitIgnoredPathsBytesSafetyTests(unittest.TestCase):
    """git_ignored_paths must be bytes/surrogateescape-safe (path_inventory SSOT)."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="direct-ignored-bytes-")
        self.repo = pathlib.Path(self.tmp) / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "-C", str(self.repo), "init", "--initial-branch=main"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "t@t.t"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "t"],
            check=True,
            capture_output=True,
        )
        (self.repo / "tracked.txt").write_text("ok\n")
        (self.repo / ".gitignore").write_text("ignored.txt\nbad-*\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "seed"],
            check=True,
            capture_output=True,
        )
        (self.repo / "ignored.txt").write_text("secret\n")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lone_surrogate_path_does_not_raise_unicode_encode(self) -> None:
        from groklib.modes.direct_finalize import git_ignored_paths

        # Lone surrogate as produced by surrogateescape decode of non-UTF-8 bytes.
        surrogate_path = "bad-\udcff-name.txt"
        # Must not raise UnicodeEncodeError; classify or return a set.
        try:
            result = git_ignored_paths(
                self.repo, {"tracked.txt", "ignored.txt", surrogate_path}
            )
        except UnicodeEncodeError as exc:
            self.fail("git_ignored_paths raised UnicodeEncodeError: {}".format(exc))
        self.assertIn("ignored.txt", result)
        self.assertNotIn("tracked.txt", result)

    def test_uses_bytes_runner_with_stdin(self) -> None:
        from unittest import mock

        from groklib.modes.direct_finalize import git_ignored_paths

        completed = mock.Mock()
        completed.returncode = 0
        completed.stdout = b"ignored.txt\0"
        completed.stderr = b""
        fake = mock.Mock(return_value=completed)
        with mock.patch("groklib.worktree._run_git_bytes", fake):
            result = git_ignored_paths(self.repo, {"ignored.txt", "tracked.txt"})
        self.assertEqual(result, {"ignored.txt"})
        fake.assert_called()
        kwargs = fake.call_args.kwargs
        self.assertIn("input_bytes", kwargs)
        self.assertIsInstance(kwargs["input_bytes"], (bytes, bytearray))
        self.assertIn(b"ignored.txt", kwargs["input_bytes"])

    def test_real_non_utf8_path_when_platform_allows(self) -> None:
        from groklib.modes.direct_finalize import git_ignored_paths

        bad_name = b"bad-\xff-name.txt"
        try:
            path_bytes = os.fsencode(str(self.repo)) + b"/" + bad_name
            fd = os.open(path_bytes, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, b"payload\n")
            finally:
                os.close(fd)
        except OSError as exc:
            self.skipTest(
                "platform cannot create invalid UTF-8 pathnames: {}".format(exc)
            )
        # Surrogate-escaped inventory path token.
        sur = bad_name.decode("utf-8", errors="surrogateescape")
        try:
            result = git_ignored_paths(self.repo, {sur, "ignored.txt"})
        except UnicodeEncodeError as exc:
            self.fail("real non-UTF-8 path raised UnicodeEncodeError: {}".format(exc))
        self.assertIn("ignored.txt", result)




if __name__ == "__main__":
    unittest.main()
