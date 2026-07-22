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

    def test_list_ignored_untracked_paths_collapses_ignored_directories(self) -> None:
        # Issue #7: without --directory, node_modules-scale trees blow the 30s
        # git timeout. Collapsed inventory must list the tree once, not leaves.
        (self.repo / ".gitignore").write_text("node_modules/\n*.local\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "ignore bulk trees")
        nested = self.repo / "node_modules" / "pkg"
        nested.mkdir(parents=True)
        (nested / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
        (self.repo / "creds.local").write_text("SECRET=x\n", encoding="utf-8")
        paths = path_inventory.list_ignored_untracked_paths(self.repo)
        self.assertTrue(
            any(p.rstrip("/") == "node_modules" for p in paths),
            paths,
        )
        self.assertFalse(
            any("index.js" in p for p in paths),
            "ignored directory inventory must not expand non-deny leaves: {}".format(paths),
        )
        self.assertIn("creds.local", paths)

    def test_list_ignored_expands_protected_leaves_under_collapsed_dirs(self) -> None:
        # Codex PR #9: secrets/ collapse must still surface secrets/id_rsa so
        # deny globs match after --directory (otherwise protected-path-write misses).
        (self.repo / ".gitignore").write_text("secrets/\nnode_modules/\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "ignore secrets tree")
        secrets = self.repo / "secrets"
        secrets.mkdir()
        (secrets / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
        (secrets / "readme.txt").write_text("not a secret leaf name\n", encoding="utf-8")
        nm = self.repo / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("ok\n", encoding="utf-8")
        paths = path_inventory.list_ignored_untracked_paths(self.repo)
        self.assertTrue(
            any(p.rstrip("/") == "secrets" for p in paths),
            "collapsed secrets/ dir should remain: {}".format(paths),
        )
        self.assertIn(
            "secrets/id_rsa",
            paths,
            "deny-scoped leaf under ignored dir must be expanded: {}".format(paths),
        )
        self.assertFalse(
            any(p.endswith("readme.txt") for p in paths),
            "non-deny leaves under collapsed dirs must stay collapsed: {}".format(paths),
        )
        self.assertFalse(
            any("index.js" in p for p in paths),
            "bulk cache non-deny leaves must stay collapsed: {}".format(paths),
        )

    def test_list_ignored_drops_untracked_parent_of_nested_ignore(self) -> None:
        # --directory may emit other/ when only other/__pycache__/ is ignored;
        # check-ignore filter must drop the non-ignored parent (scope byproducts).
        (self.repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "ignore pycache only")
        pycache = self.repo / "other" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "x.pyc").write_bytes(b"\0")
        paths = path_inventory.list_ignored_untracked_paths(self.repo)
        self.assertFalse(
            any(p.rstrip("/") == "other" for p in paths),
            "non-ignored parent must not appear: {}".format(paths),
        )
        self.assertTrue(
            any("__pycache__" in p for p in paths),
            paths,
        )

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

    def test_list_ignored_untracked_paths_passes_directory_flag(self) -> None:
        ls_done = mock.Mock()
        ls_done.returncode = 0
        ls_done.stdout = b"node_modules/\0"
        ls_done.stderr = b""
        ci_done = mock.Mock()
        ci_done.returncode = 0
        ci_done.stdout = b"node_modules/\0"
        ci_done.stderr = b""
        fake = mock.Mock(side_effect=[ls_done, ci_done])
        with mock.patch("groklib.worktree._run_git_bytes", fake):
            paths = path_inventory.list_ignored_untracked_paths(self.repo)
        self.assertEqual(paths, ["node_modules/"])
        self.assertEqual(fake.call_count, 2)
        ls_args = list(fake.call_args_list[0][0][1])
        self.assertEqual(
            ls_args,
            [
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
            ],
        )
        ci_args = list(fake.call_args_list[1][0][1])
        self.assertEqual(ci_args, ["check-ignore", "--stdin", "-z"])

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
