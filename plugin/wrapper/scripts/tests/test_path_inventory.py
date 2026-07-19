# wrapper/scripts/tests/test_path_inventory.py
#
# Focused coverage for the shared NUL-safe path inventory (default core.quotePath).

import pathlib
import shutil
import subprocess
import tempfile
import unittest

from groklib import path_inventory
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
