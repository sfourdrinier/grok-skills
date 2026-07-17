# wrapper/scripts/tests/test_direct_protect.py
#
# Unit coverage for direct-mode protected-path snapshot + restore (Task 7.1b).
# Integration disk-state tests live in test_mode_direct.DirectProtectedPathRollbackTests.

import pathlib
import tempfile
import unittest

from groklib.modes import direct_protect


class DirectProtectSnapshotRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="direct-protect-")
        self.repo = pathlib.Path(self.tmp) / "repo"
        self.run_dir = pathlib.Path(self.tmp) / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        (self.repo / ".git" / "hooks").mkdir(parents=True)
        (self.repo / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n")

    def test_snapshot_copies_env_and_git_paths(self) -> None:
        env_bytes = b"DEBUG=false\n"
        (self.repo / ".env").write_bytes(env_bytes)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn(".env", snap.entries)
        self.assertTrue(snap.entries[".env"].snapshotted)
        self.assertIn(".git/config", snap.entries)
        self.assertTrue((snap.snapshot_dir / ".env").is_file())
        self.assertEqual((snap.snapshot_dir / ".env").read_bytes(), env_bytes)
        mode = (snap.snapshot_dir / ".env").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        dir_mode = snap.snapshot_dir.stat().st_mode & 0o777
        self.assertEqual(dir_mode, 0o700)

    def test_restore_overwrites_modified_env(self) -> None:
        original = b"SECRET=keep\n"
        (self.repo / ".env").write_bytes(original)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / ".env").write_bytes(original + b"LEAK=1\n")
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[".env"]
        )
        self.assertEqual(self.repo.joinpath(".env").read_bytes(), original)
        self.assertIn(".env", result.restored)
        self.assertEqual(result.unrestored, [])
        self.assertEqual(result.errors, [])

    def test_restore_deletes_created_protected_file(self) -> None:
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        pem = self.repo / "new.pem"
        pem.write_text("-----BEGIN FAKE-----\n", encoding="utf-8")
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=["new.pem"]
        )
        self.assertFalse(pem.exists())
        self.assertIn("new.pem", result.restored)
        self.assertEqual(result.errors, [])

    def test_restore_does_not_touch_non_offenders(self) -> None:
        (self.repo / ".env").write_bytes(b"A=1\n")
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / ".env").write_bytes(b"A=2\n")
        (self.repo / "src" / "app.py").write_text("print('edited')\n")
        direct_protect.restore_protected_paths(self.repo, snap, offenders=[".env"])
        self.assertEqual((self.repo / "src" / "app.py").read_text(), "print('edited')\n")
        self.assertEqual((self.repo / ".env").read_bytes(), b"A=1\n")

    def test_over_cap_recorded_unsnapshottable(self) -> None:
        (self.repo / ".env").write_bytes(b"Z" * 100)
        snap = direct_protect.snapshot_protected_paths(
            self.repo, self.run_dir, max_total_bytes=40
        )
        entry = snap.entries[".env"]
        self.assertTrue(entry.existed)
        self.assertFalse(entry.snapshotted)
        self.assertEqual(entry.reason, "over-cap")
        (self.repo / ".env").write_bytes(b"TAMPER\n")
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[".env"]
        )
        self.assertNotIn(".env", result.restored)
        self.assertIn(".env", result.unrestored)
        self.assertIn("too large to roll back", result.honest_message or "")
        self.assertEqual((self.repo / ".env").read_bytes(), b"TAMPER\n")

    def test_restore_failure_surfaced_not_swallowed(self) -> None:
        original = b"keep\n"
        (self.repo / ".env").write_bytes(original)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        (self.repo / ".env").write_bytes(b"changed\n")
        # Replace snapshot file with a directory so copy fails.
        snap_file = snap.snapshot_dir / ".env"
        snap_file.unlink()
        snap_file.mkdir()
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[".env"]
        )
        self.assertNotIn(".env", result.restored)
        self.assertIn(".env", result.unrestored)
        self.assertTrue(result.errors)
        self.assertEqual(result.errors[0]["path"], ".env")
        self.assertIn("error", result.errors[0])

    def test_does_not_walk_git_objects(self) -> None:
        objects = self.repo / ".git" / "objects" / "ab"
        objects.mkdir(parents=True)
        (objects / "cd").write_bytes(b"pack-like")
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        for rel in snap.entries:
            self.assertFalse(rel.startswith(".git/objects"), rel)

    def test_snapshot_includes_git_refs(self) -> None:
        ref = self.repo / ".git" / "refs" / "heads" / "main"
        ref.parent.mkdir(parents=True)
        ref.write_text("0" * 40 + "\n")
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn(".git/refs/heads/main", snap.entries)
        self.assertTrue(snap.entries[".git/refs/heads/main"].snapshotted)

    def test_restore_reverts_moved_ref(self) -> None:
        ref = self.repo / ".git" / "refs" / "heads" / "main"
        ref.parent.mkdir(parents=True)
        original = "1" * 40 + "\n"
        ref.write_text(original)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        ref.write_text("f" * 40 + "\n")  # branch moved to a planted commit
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[".git/refs/heads/main"]
        )
        self.assertEqual(ref.read_text(), original)
        self.assertIn(".git/refs/heads/main", result.restored)
        self.assertEqual(result.errors, [])

    def test_restore_deletes_created_ref(self) -> None:
        (self.repo / ".git" / "refs" / "heads").mkdir(parents=True)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        evil = self.repo / ".git" / "refs" / "heads" / "evil"
        evil.write_text("f" * 40 + "\n")
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[".git/refs/heads/evil"]
        )
        self.assertFalse(evil.exists())
        self.assertIn(".git/refs/heads/evil", result.restored)

    def test_is_snapshot_scope_covers_refs_and_packed_refs(self) -> None:
        self.assertTrue(direct_protect.is_snapshot_scope(".git/refs/heads/main"))
        self.assertTrue(direct_protect.is_snapshot_scope(".git/packed-refs"))
        # index/objects remain detect-only (no auto-delete on restore).
        self.assertFalse(direct_protect.is_snapshot_scope(".git/index"))
        self.assertFalse(direct_protect.is_snapshot_scope(".git/objects/ab/cd"))


class DirectGitGuardAndDenyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="direct-guard-")
        self.repo = pathlib.Path(self.tmp) / "repo"
        (self.repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.repo / ".git" / "refs" / "heads" / "main").write_text("1" * 40 + "\n")

    def test_guard_detects_moved_ref(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        baseline = capture_git_dir_guard(self.repo)
        (self.repo / ".git" / "refs" / "heads" / "main").write_text("f" * 40 + "\n")
        after = capture_git_dir_guard(self.repo)
        self.assertIn(".git/refs/heads/main", _changed_paths(baseline, after))

    def test_guard_detects_created_ref(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        baseline = capture_git_dir_guard(self.repo)
        (self.repo / ".git" / "refs" / "heads" / "evil").write_text("f" * 40 + "\n")
        after = capture_git_dir_guard(self.repo)
        self.assertIn(".git/refs/heads/evil", _changed_paths(baseline, after))

    def test_expanded_deny_globs(self) -> None:
        from groklib.modes.direct_finalize import path_matches_deny

        for p in ("id_rsa", "id_ed25519", ".netrc", ".npmrc", ".envrc", "key.p8",
                  "sub/dir/id_ecdsa", "deep/nested/.npmrc"):
            self.assertTrue(path_matches_deny(p), p)
        for p in ("src/app.py", "README.md", "package.json"):
            self.assertFalse(path_matches_deny(p), p)


if __name__ == "__main__":
    unittest.main()
