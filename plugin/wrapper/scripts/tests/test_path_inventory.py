# wrapper/scripts/tests/test_path_inventory.py
#
# Focused coverage for the shared NUL-safe path inventory (default core.quotePath).

import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from groklib import GrokWrapperError, path_inventory
from tests import gitfixtures


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class PathInventoryQuotePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-path-inv-")
        self.repo = gitfixtures.make_repo(self.tmp)
        _git(self.repo, "config", "core.quotePath", "true")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_working_tree_changed_paths_real_non_ascii(self) -> None:
        (self.repo / "café.txt").write_text("untracked\n", encoding="utf-8")
        with (self.repo / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("tracked edit\n")
        paths = path_inventory.list_working_tree_changed_paths(self.repo, "HEAD")
        self.assertIn("café.txt", paths)
        self.assertIn("a.txt", paths)
        self.assertFalse(any("\\303" in p or p.startswith('"') for p in paths))

    def test_list_ignored_untracked_paths_real_non_ascii(self) -> None:
        (self.repo / ".gitignore").write_text("ignored-*\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "ignore pattern")
        (self.repo / "ignored-café.tmp").write_text("secret\n", encoding="utf-8")
        paths = path_inventory.list_ignored_untracked_paths(self.repo)
        self.assertIn("ignored-café.tmp", paths)
        self.assertFalse(any("\\303" in p or p.startswith('"') for p in paths))

    def test_decode_nul_paths_does_not_cunquote(self) -> None:
        # Raw -z already carries real UTF-8 bytes; do not treat backslash as escape.
        raw = "café.txt".encode("utf-8") + b"\0" + b'"caf\\303\\251.txt"\0'
        decoded = path_inventory.decode_nul_paths(raw)
        self.assertEqual(decoded[0], "café.txt")
        self.assertEqual(decoded[1], '"caf\\303\\251.txt"')

    def test_decode_nul_paths_preserves_invalid_utf8_via_surrogateescape(self) -> None:
        # Unit bytes path must always run; invalid UTF-8 survives as surrogates.
        raw = b"ok.txt\0" + b"bad-\xff-name.txt\0"
        decoded = path_inventory.decode_nul_paths(raw)
        self.assertEqual(decoded[0], "ok.txt")
        self.assertEqual(
            decoded[1].encode("utf-8", errors="surrogateescape"),
            b"bad-\xff-name.txt",
        )
        # Text-mode replacement would destroy the byte; bytes path must not.
        text_replaced = path_inventory.decode_nul_paths("bad-�-name.txt\0")
        self.assertNotEqual(decoded[1], text_replaced[0])


class PathInventoryBytesRunnerTests(unittest.TestCase):
    """Inventory must invoke git in bytes mode so surrogateescape is reachable."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-path-inv-bytes-")
        self.repo = gitfixtures.make_repo(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_ls_files_uses_bytes_git_runner(self) -> None:
        # Inject raw invalid-UTF-8 path bytes as if git -z emitted them.
        completed = mock.Mock()
        completed.returncode = 0
        completed.stdout = b"tracked.txt\0" + b"bad-\xff-name.txt\0"
        completed.stderr = b""
        fake = mock.Mock(return_value=completed)
        with mock.patch("groklib.worktree._run_git_bytes", fake):
            paths = path_inventory.list_ls_files(self.repo)
        self.assertIn("tracked.txt", paths)
        self.assertTrue(
            any(
                p.encode("utf-8", errors="surrogateescape") == b"bad-\xff-name.txt"
                for p in paths
            ),
            paths,
        )
        fake.assert_called()
        args = fake.call_args[0][1]
        self.assertIn("-z", list(args))
        # Must not have gone through the text-decoding _run_git path.
        self.assertIsInstance(completed.stdout, (bytes, bytearray))

    def test_list_diff_name_only_fail_closed_on_fatal(self) -> None:
        completed = mock.Mock()
        completed.returncode = 128
        completed.stdout = b""
        completed.stderr = b"fatal: bad revision"
        fake = mock.Mock(return_value=completed)
        with mock.patch("groklib.worktree._run_git_bytes", fake):
            with self.assertRaises(GrokWrapperError) as cm:
                path_inventory.list_diff_name_only(
                    self.repo, "HEAD", error_class="artifact-generation-failure"
                )
        self.assertEqual(cm.exception.error_class, "artifact-generation-failure")

    def test_real_invalid_utf8_path_when_platform_allows(self) -> None:
        """Linux-compatible: create non-UTF-8 path bytes when the OS permits."""
        import os

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

        paths = path_inventory.list_ls_files(
            self.repo, "--others", "--exclude-standard"
        )
        self.assertTrue(
            any(
                p.encode("utf-8", errors="surrogateescape") == bad_name for p in paths
            ),
            paths,
        )
        try:
            path_inventory.list_working_tree_changed_paths(self.repo, "HEAD")
        except UnicodeDecodeError as exc:
            self.fail("inventory raised UnicodeDecodeError: {}".format(exc))
        except GrokWrapperError:
            # Other classified failures are fine; decode corruption is not.
            pass
