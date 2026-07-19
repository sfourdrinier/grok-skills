# wrapper/scripts/tests/test_direct_protect_abort.py
#
# Abort-path protected-path rollback coverage (restore_protected_on_abort).
# Snapshot/restore unit tests: test_direct_protect.py
# Gitfile/modules discovery: test_direct_protect_gitfile.py

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from groklib.modes import direct_protect


class DirectAbortSweepTests(unittest.TestCase):
    """restore_protected_on_abort: rollback on abnormal exit (reviews 2/3/5)."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="direct-abort-")
        self.repo = pathlib.Path(self.tmp) / "repo"
        self.run_dir = pathlib.Path(self.tmp) / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.email", "t@t.t")
        self._git("config", "user.name", "t")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n")
        self._git("add", "-A")
        self._git("commit", "-m", "seed")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        )

    def _baseline(self):
        from groklib import worktree_escape
        from groklib.modes.direct_finalize import capture_git_dir_guard

        return (
            worktree_escape.repo_change_fingerprint(self.repo),
            capture_git_dir_guard(self.repo),
        )

    def test_sweep_deletes_created_env_after_abort(self) -> None:
        from groklib.modes.direct_finalize import restore_protected_on_abort

        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / ".env").write_text("SECRET=leak\n")  # Grok wrote then aborted
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertFalse((self.repo / ".env").exists())
        self.assertIn(".env", res["restored"])

    def test_sweep_restores_modified_env_after_abort(self) -> None:
        from groklib.modes.direct_finalize import restore_protected_on_abort

        (self.repo / ".env").write_text("KEEP=1\n")
        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / ".env").write_text("KEEP=1\nLEAK=2\n")
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual((self.repo / ".env").read_text(), "KEEP=1\n")
        self.assertIn(".env", res["restored"])

    def test_sweep_restores_moved_ref_after_abort(self) -> None:
        from groklib.modes.direct_finalize import restore_protected_on_abort

        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        ref = self.repo / ".git" / "refs" / "heads" / "main"
        original = ref.read_text()
        ref.write_text("f" * 40 + "\n")  # branch moved to a planted commit
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(ref.read_text(), original)
        self.assertIn(".git/refs/heads/main", res["restored"])

    def test_sweep_restores_same_stat_ignored_env_after_abort(self) -> None:
        # Gitignored protected .env rewritten at same size with restored mtime must
        # still be rolled back (content-hash fingerprint), not missed as unchanged.
        from groklib.modes.direct_finalize import restore_protected_on_abort

        (self.repo / ".gitignore").write_text(".env\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore env")
        env = self.repo / ".env"
        original = b"SECRET=keep-me-xx\n"
        env.write_bytes(original)
        os.chmod(str(env), 0o600)
        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        st = env.stat()
        env.write_bytes(b"SECRET=leaked-now\n")
        os.chmod(str(env), 0o600)
        os.utime(str(env), ns=(st.st_atime_ns, st.st_mtime_ns))
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(env.read_bytes(), original)
        self.assertIn(".env", res["restored"])

    def test_sweep_restores_nested_hook_after_abort(self) -> None:
        from groklib.modes.direct_finalize import restore_protected_on_abort

        nested = self.repo / ".git" / "hooks" / "vendor" / "pre-commit"
        nested.parent.mkdir(parents=True)
        original = b"#!/bin/sh\necho good\n"
        nested.write_bytes(original)
        os.chmod(str(nested), 0o755)
        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        nested.write_bytes(b"#!/bin/sh\necho evil\n")
        os.chmod(str(nested), 0o644)
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(nested.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(os.stat(str(nested)).st_mode), 0o755)
        self.assertIn(".git/hooks/vendor/pre-commit", res["restored"])

    def test_sweep_noop_when_only_source_changed(self) -> None:
        from groklib.modes.direct_finalize import restore_protected_on_abort

        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / "src" / "app.py").write_text("print('edited')\n")
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(res["restored"], [])
        self.assertEqual((self.repo / "src" / "app.py").read_text(), "print('edited')\n")

    def test_sweep_restores_nested_vendor_and_modules_after_abort(self) -> None:
        from groklib.modes.direct_finalize import restore_protected_on_abort

        nested_git = self.repo / "vendor" / "lib" / ".git"
        (nested_git / "hooks").mkdir(parents=True)
        head = nested_git / "HEAD"
        original_head = "ref: refs/heads/main\n"
        head.write_text(original_head)
        (nested_git / "config").write_text("[core]\n")
        hook = nested_git / "hooks" / "pre-commit"
        original_hook = b"#!/bin/sh\necho good\n"
        hook.write_bytes(original_hook)
        mod_hooks = self.repo / ".git" / "modules" / "sub" / "hooks"
        mod_hooks.mkdir(parents=True)
        (self.repo / ".git" / "modules" / "sub" / "HEAD").write_text(
            "ref: refs/heads/main\n"
        )
        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        hook.write_bytes(b"#!/bin/sh\necho evil\n")
        head.write_text("ref: refs/heads/evil\n")
        planted = mod_hooks / "pre-commit"
        planted.write_bytes(b"#!/bin/sh\necho modules-plant\n")
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertEqual(head.read_text(), original_head)
        self.assertFalse(planted.exists())
        self.assertIn("vendor/lib/.git/hooks/pre-commit", res["restored"])
        self.assertIn("vendor/lib/.git/HEAD", res["restored"])
        self.assertIn(".git/modules/sub/hooks/pre-commit", res["restored"])

    def test_sweep_restores_env_when_rediff_fails_after_git_corruption(self) -> None:
        # When repo_change_fingerprint fails (e.g. .git/HEAD rewritten mid-flight),
        # deny-listed checkout paths like .env must still be restored from the
        # protected snapshot - not skipped because the full changed-set is untrusted.
        from unittest import mock

        from groklib import worktree_escape
        from groklib.modes.direct_finalize import restore_protected_on_abort

        (self.repo / ".env").write_text("KEEP=1\n", encoding="utf-8")
        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / ".env").write_text("KEEP=1\nLEAK=2\n", encoding="utf-8")
        # Corrupt git metadata that the git-dir guard can still recover, while the
        # working-tree fingerprint path is forced to fail closed.
        head = self.repo / ".git" / "HEAD"
        original_head = head.read_text(encoding="utf-8")
        head.write_text("ref: refs/heads/evil\n", encoding="utf-8")

        def _boom(_repo):
            raise OSError("simulated fingerprint failure after git metadata corruption")

        with mock.patch.object(worktree_escape, "repo_change_fingerprint", side_effect=_boom):
            res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(
            (self.repo / ".env").read_text(encoding="utf-8"),
            "KEEP=1\n",
            ".env must be restored from snapshot when re-diff fails",
        )
        self.assertIn(".env", res["restored"])
        self.assertEqual(head.read_text(encoding="utf-8"), original_head)
        self.assertIn(".git/HEAD", res["restored"])

    def test_sweep_restores_env_when_rediff_fails_after_head_corruption(self) -> None:
        # Re-diff can fail when Grok corrupts .git/HEAD; git-dir guard still restores
        # HEAD, but deny-listed .env must not stay dirty with a silent clean summary.
        from groklib.modes.direct_finalize import restore_protected_on_abort

        env = self.repo / ".env"
        original = "KEEP=1\n"
        env.write_text(original, encoding="utf-8")
        base_fp, base_git = self._baseline()
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        original_head = (self.repo / ".git" / "HEAD").read_text(encoding="utf-8")
        env.write_text("KEEP=1\nLEAK=yes\n", encoding="utf-8")
        (self.repo / ".git" / "HEAD").write_text("not-a-valid-ref\n", encoding="utf-8")
        res = restore_protected_on_abort(self.repo, base_fp, base_git, snap)
        self.assertEqual(env.read_text(encoding="utf-8"), original)
        self.assertIn(".env", res["restored"])
        self.assertEqual(
            (self.repo / ".git" / "HEAD").read_text(encoding="utf-8"), original_head
        )
        self.assertIn(".git/HEAD", res["restored"])


if __name__ == "__main__":
    unittest.main()
