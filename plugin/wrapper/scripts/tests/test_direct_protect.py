# wrapper/scripts/tests/test_direct_protect.py
#
# Unit coverage for direct-mode protected-path snapshot + restore (Task 7.1b).
# Integration disk-state tests live in test_mode_direct.DirectProtectedPathRollbackTests.

import os
import pathlib
import stat
import subprocess
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

    def test_restore_recreates_preexisting_env_symlink(self) -> None:
        (self.repo / "secret-target").write_text("SECRET=1\n")
        (self.repo / ".env").symlink_to("secret-target")
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn(".env", snap.entries)
        self.assertEqual(snap.entries[".env"].symlink_target, "secret-target")
        # Grok replaces the symlink with a regular file.
        (self.repo / ".env").unlink()
        (self.repo / ".env").write_text("LEAK=1\n")
        result = direct_protect.restore_protected_paths(self.repo, snap, offenders=[".env"])
        self.assertIn(".env", result.restored)
        self.assertTrue((self.repo / ".env").is_symlink())
        self.assertEqual(os.readlink(str(self.repo / ".env")), "secret-target")

    def test_restore_preserves_0600_mode(self) -> None:
        env = self.repo / ".env"
        env.write_bytes(b"SECRET=1\n")
        os.chmod(str(env), 0o600)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertEqual(snap.entries[".env"].mode, 0o600)
        env.write_bytes(b"LEAK=1\n")
        os.chmod(str(env), 0o644)  # Grok widened it
        direct_protect.restore_protected_paths(self.repo, snap, offenders=[".env"])
        self.assertEqual(stat.S_IMODE(os.stat(str(env)).st_mode), 0o600)

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

    def test_snapshot_includes_nested_hooks(self) -> None:
        nested = self.repo / ".git" / "hooks" / "vendor" / "pre-commit"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"#!/bin/sh\necho nested\n")
        os.chmod(str(nested), 0o755)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        rel = ".git/hooks/vendor/pre-commit"
        self.assertIn(rel, snap.entries)
        self.assertTrue(snap.entries[rel].snapshotted)
        self.assertEqual((snap.snapshot_dir / rel).read_bytes(), nested.read_bytes())

    def test_restore_reverts_nested_hook_bytes_and_mode(self) -> None:
        nested = self.repo / ".git" / "hooks" / "vendor" / "pre-commit"
        nested.parent.mkdir(parents=True)
        original = b"#!/bin/sh\necho good\n"
        nested.write_bytes(original)
        os.chmod(str(nested), 0o755)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        nested.write_bytes(b"#!/bin/sh\necho evil\n")
        os.chmod(str(nested), 0o644)
        rel = ".git/hooks/vendor/pre-commit"
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[rel]
        )
        self.assertEqual(nested.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(os.stat(str(nested)).st_mode), 0o755)
        self.assertIn(rel, result.restored)
        self.assertEqual(result.errors, [])

    def test_restore_deletes_created_nested_hook(self) -> None:
        (self.repo / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        nested = self.repo / ".git" / "hooks" / "vendor" / "post-commit"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"#!/bin/sh\necho planted\n")
        rel = ".git/hooks/vendor/post-commit"
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[rel]
        )
        self.assertFalse(nested.exists())
        self.assertIn(rel, result.restored)

    def test_snapshot_includes_nested_vendor_gitdir(self) -> None:
        nested_git = self.repo / "vendor" / "lib" / ".git"
        (nested_git / "hooks").mkdir(parents=True)
        (nested_git / "HEAD").write_text("ref: refs/heads/main\n")
        (nested_git / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
        hook = nested_git / "hooks" / "pre-commit"
        hook.write_bytes(b"#!/bin/sh\necho nested-repo\n")
        os.chmod(str(hook), 0o755)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        rel_hook = "vendor/lib/.git/hooks/pre-commit"
        rel_head = "vendor/lib/.git/HEAD"
        self.assertIn(rel_hook, snap.entries)
        self.assertIn(rel_head, snap.entries)
        self.assertTrue(snap.entries[rel_hook].snapshotted)

    def test_restore_nested_vendor_hook_and_head(self) -> None:
        nested_git = self.repo / "vendor" / "lib" / ".git"
        (nested_git / "hooks").mkdir(parents=True)
        head = nested_git / "HEAD"
        original_head = "ref: refs/heads/main\n"
        head.write_text(original_head)
        (nested_git / "config").write_text("[core]\n")
        hook = nested_git / "hooks" / "pre-commit"
        original_hook = b"#!/bin/sh\necho good\n"
        hook.write_bytes(original_hook)
        os.chmod(str(hook), 0o755)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        # Plant evil hook + rewrite HEAD (live nested-repo attack).
        hook.write_bytes(b"#!/bin/sh\necho evil\n")
        head.write_text("ref: refs/heads/evil\n")
        planted = nested_git / "hooks" / "post-commit"
        planted.write_bytes(b"#!/bin/sh\necho planted\n")
        result = direct_protect.restore_protected_paths(
            self.repo,
            snap,
            offenders=[
                "vendor/lib/.git/hooks/pre-commit",
                "vendor/lib/.git/HEAD",
                "vendor/lib/.git/hooks/post-commit",
            ],
        )
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertEqual(head.read_text(), original_head)
        self.assertFalse(planted.exists())
        self.assertIn("vendor/lib/.git/hooks/pre-commit", result.restored)
        self.assertIn("vendor/lib/.git/HEAD", result.restored)
        self.assertIn("vendor/lib/.git/hooks/post-commit", result.restored)

    def test_snapshot_and_restore_root_modules_hook(self) -> None:
        mod_hooks = self.repo / ".git" / "modules" / "sub" / "hooks"
        mod_hooks.mkdir(parents=True)
        (self.repo / ".git" / "modules" / "sub" / "HEAD").write_text(
            "ref: refs/heads/main\n"
        )
        (self.repo / ".git" / "modules" / "sub" / "config").write_text("[core]\n")
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        planted = mod_hooks / "pre-commit"
        planted.write_bytes(b"#!/bin/sh\necho modules-plant\n")
        rel = ".git/modules/sub/hooks/pre-commit"
        self.assertTrue(direct_protect.is_snapshot_scope(rel))
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[rel]
        )
        self.assertFalse(planted.exists())
        self.assertIn(rel, result.restored)

    def test_is_sensitive_git_relative_covers_nested_and_modules(self) -> None:
        self.assertTrue(
            direct_protect.is_sensitive_git_relative("vendor/lib/.git/hooks/pre-commit")
        )
        self.assertTrue(
            direct_protect.is_sensitive_git_relative("vendor/lib/.git/HEAD")
        )
        self.assertTrue(
            direct_protect.is_sensitive_git_relative(".git/modules/sub/hooks/x")
        )
        self.assertTrue(direct_protect.is_sensitive_git_relative(".git/config"))
        self.assertFalse(
            direct_protect.is_sensitive_git_relative("vendor/lib/.git/index")
        )
        self.assertFalse(
            direct_protect.is_sensitive_git_relative("vendor/lib/.git/objects/ab/cd")
        )

    def test_discovery_fail_closed_on_overflow(self) -> None:
        from groklib import GrokWrapperError

        # Tiny bound must fail closed rather than silently skip nested gitdirs.
        with self.assertRaises(GrokWrapperError) as cm:
            direct_protect.discover_workspace_git_roots(self.repo, max_discovery=0)
        self.assertEqual(cm.exception.error_class, "protected-path-write")

    def test_gitfile_outside_workspace_not_inventoried(self) -> None:
        # Honest linked-worktree limit: gitfile pointing outside workspace is not
        # walked as nested protected content (common dir often lives outside).
        linked = self.repo / "linked-wt"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: /tmp/outside-common/.git/worktrees/x\n")
        roots = direct_protect.discover_workspace_git_roots(self.repo)
        self.assertFalse(any(rel == "linked-wt/.git" for rel, _ in roots))

    def _seed_in_workspace_gitfile(
        self, gitfile: pathlib.Path, common: pathlib.Path
    ) -> tuple:
        """Create gitfile -> in-workspace common with HEAD/config/hook."""
        import shutil

        # setUp may have created a free-standing .git directory; replace with gitfile.
        if gitfile.exists() or gitfile.is_symlink():
            if gitfile.is_dir() and not gitfile.is_symlink():
                shutil.rmtree(str(gitfile))
            else:
                gitfile.unlink()
        (common / "hooks").mkdir(parents=True, exist_ok=True)
        head = common / "HEAD"
        config = common / "config"
        hook = common / "hooks" / "pre-commit"
        original_head = "ref: refs/heads/main\n"
        original_config = "[core]\n\trepositoryformatversion = 0\n"
        original_hook = b"#!/bin/sh\necho good\n"
        head.write_text(original_head)
        config.write_text(original_config)
        hook.write_bytes(original_hook)
        os.chmod(str(hook), 0o755)
        gitfile.parent.mkdir(parents=True, exist_ok=True)
        # Relative gitdir: target keeps working if the test tree is moved.
        rel_target = os.path.relpath(str(common), str(gitfile.parent))
        gitfile.write_text("gitdir: {}\n".format(rel_target))
        return original_head, original_config, original_hook, head, config, hook

    def test_root_gitfile_in_workspace_common_snapshot_restore(self) -> None:
        # Root .git is a FILE pointing at an in-workspace common dir. Snapshot and
        # restore must use the actual gitdir, never repo/.git/<child>.
        common = self.repo / ".linked-common"
        (
            original_head,
            original_config,
            original_hook,
            head,
            config,
            hook,
        ) = self._seed_in_workspace_gitfile(self.repo / ".git", common)

        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn(".git/HEAD", snap.entries)
        self.assertTrue(snap.entries[".git/HEAD"].snapshotted, snap.entries[".git/HEAD"])
        self.assertIn(".git/config", snap.entries)
        self.assertTrue(snap.entries[".git/config"].snapshotted)
        self.assertIn(".git/hooks/pre-commit", snap.entries)
        self.assertTrue(snap.entries[".git/hooks/pre-commit"].snapshotted)
        self.assertEqual(snap.entries[".git/hooks/pre-commit"].mode, 0o755)
        # Abs map must point into the common dir, not under the gitfile path.
        abs_head = pathlib.Path(snap.abs_paths[".git/HEAD"])
        self.assertEqual(abs_head.resolve(), head.resolve())
        self.assertFalse(str(abs_head).endswith(str(self.repo / ".git" / "HEAD")))

        head.write_text("ref: refs/heads/evil\n")
        config.write_text("[core]\n\tevil = 1\n")
        hook.write_bytes(b"#!/bin/sh\necho evil\n")
        os.chmod(str(hook), 0o644)
        planted = common / "hooks" / "post-commit"
        planted.write_bytes(b"#!/bin/sh\necho planted\n")

        result = direct_protect.restore_protected_paths(
            self.repo,
            snap,
            offenders=[
                ".git/HEAD",
                ".git/config",
                ".git/hooks/pre-commit",
                ".git/hooks/post-commit",
            ],
        )
        self.assertEqual(head.read_text(), original_head)
        self.assertEqual(config.read_text(), original_config)
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertEqual(stat.S_IMODE(os.stat(str(hook)).st_mode), 0o755)
        self.assertFalse(planted.exists())
        for rel in (
            ".git/HEAD",
            ".git/config",
            ".git/hooks/pre-commit",
            ".git/hooks/post-commit",
        ):
            self.assertIn(rel, result.restored, result)
        self.assertEqual(result.unrestored, [])
        self.assertEqual(result.errors, [])
        # Never materialize a directory tree under the gitfile path.
        self.assertTrue((self.repo / ".git").is_file())

    def test_nested_vendor_gitfile_in_workspace_target_snapshot_restore(self) -> None:
        common = self.repo / "vendor" / "lib" / ".actual-git"
        gitfile = self.repo / "vendor" / "lib" / ".git"
        (
            original_head,
            _cfg,
            original_hook,
            head,
            _config,
            hook,
        ) = self._seed_in_workspace_gitfile(gitfile, common)

        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        rel_head = "vendor/lib/.git/HEAD"
        rel_hook = "vendor/lib/.git/hooks/pre-commit"
        self.assertIn(rel_head, snap.entries)
        self.assertTrue(snap.entries[rel_head].snapshotted)
        self.assertIn(rel_hook, snap.entries)
        self.assertTrue(snap.entries[rel_hook].snapshotted)
        self.assertEqual(
            pathlib.Path(snap.abs_paths[rel_head]).resolve(), head.resolve()
        )

        head.write_text("ref: refs/heads/evil\n")
        hook.write_bytes(b"#!/bin/sh\necho evil\n")
        os.chmod(str(hook), 0o600)
        planted = common / "hooks" / "post-commit"
        planted.write_bytes(b"#!/bin/sh\necho plant\n")

        result = direct_protect.restore_protected_paths(
            self.repo,
            snap,
            offenders=[rel_head, rel_hook, "vendor/lib/.git/hooks/post-commit"],
        )
        self.assertEqual(head.read_text(), original_head)
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertEqual(stat.S_IMODE(os.stat(str(hook)).st_mode), 0o755)
        self.assertFalse(planted.exists())
        self.assertIn(rel_head, result.restored)
        self.assertIn(rel_hook, result.restored)
        self.assertIn("vendor/lib/.git/hooks/post-commit", result.restored)
        self.assertEqual(result.unrestored, [])
        self.assertTrue(gitfile.is_file())

    def test_guard_uses_actual_gitfile_target_paths(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        common = self.repo / ".linked-common"
        self._seed_in_workspace_gitfile(self.repo / ".git", common)
        baseline = capture_git_dir_guard(self.repo)
        (common / "HEAD").write_text("ref: refs/heads/evil\n")
        (common / "hooks" / "post-commit").write_bytes(b"#!/bin/sh\necho x\n")
        changed = _changed_paths(baseline, capture_git_dir_guard(self.repo))
        self.assertIn(".git/HEAD", changed)
        self.assertIn(".git/hooks/post-commit", changed)


class DirectGitGuardAndDenyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="direct-guard-")
        self.repo = pathlib.Path(self.tmp) / "repo"
        (self.repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.repo / ".git" / "hooks").mkdir(parents=True)
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

    def test_guard_ignores_benign_index_and_commit_editmsg(self) -> None:
        # Regression: git rewrites .git/index on ordinary reads (git status),
        # which must NOT be a fatal protected-path-write in direct mode.
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        baseline = capture_git_dir_guard(self.repo)
        (self.repo / ".git" / "index").write_bytes(b"DIRC-fake-index\n")
        (self.repo / ".git" / "COMMIT_EDITMSG").write_text("wip\n")
        after = capture_git_dir_guard(self.repo)
        self.assertEqual(_changed_paths(baseline, after), set())

    def test_guard_detects_ref_move_with_restored_mtime(self) -> None:
        # Content-hash signature: a same-length ref move with a coalesced/restored
        # mtime still flips the set-difference (a stat-only sig would miss it).
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        ref = self.repo / ".git" / "refs" / "heads" / "main"
        ref.write_text("1" * 40 + "\n")
        st = ref.stat()
        baseline = capture_git_dir_guard(self.repo)
        ref.write_text("f" * 40 + "\n")  # same length, different SHA
        os.utime(str(ref), ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
        after = capture_git_dir_guard(self.repo)
        self.assertIn(".git/refs/heads/main", _changed_paths(baseline, after))

    def test_guard_detects_nested_new_hook(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        baseline = capture_git_dir_guard(self.repo)
        nested = self.repo / ".git" / "hooks" / "vendor" / "pre-commit"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"#!/bin/sh\necho planted\n")
        after = capture_git_dir_guard(self.repo)
        self.assertIn(".git/hooks/vendor/pre-commit", _changed_paths(baseline, after))

    def test_guard_detects_nested_hook_byte_and_mode_change(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        nested = self.repo / ".git" / "hooks" / "vendor" / "pre-commit"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"#!/bin/sh\necho good\n")
        os.chmod(str(nested), 0o755)
        st = nested.stat()
        baseline = capture_git_dir_guard(self.repo)
        nested.write_bytes(b"#!/bin/sh\necho evil\n")  # same length
        os.chmod(str(nested), 0o644)
        os.utime(str(nested), ns=(st.st_atime_ns, st.st_mtime_ns))
        after = capture_git_dir_guard(self.repo)
        self.assertIn(".git/hooks/vendor/pre-commit", _changed_paths(baseline, after))

    def test_expanded_deny_globs(self) -> None:
        from groklib.modes.direct_finalize import path_matches_deny

        for p in ("id_rsa", "id_ed25519", ".netrc", ".npmrc", ".envrc", "key.p8",
                  "sub/dir/id_ecdsa", "deep/nested/.npmrc", ".env/production",
                  ".env/staging.local", "credentials.json"):
            self.assertTrue(path_matches_deny(p), p)
        for p in ("src/app.py", "README.md", "package.json"):
            self.assertFalse(path_matches_deny(p), p)

    def test_deny_covers_nested_git_and_modules(self) -> None:
        from groklib.modes.direct_finalize import path_matches_deny

        for p in (
            "vendor/lib/.git/hooks/pre-commit",
            "vendor/lib/.git/HEAD",
            ".git/modules/sub/hooks/pre-commit",
            ".git/modules/sub/config",
        ):
            self.assertTrue(path_matches_deny(p), p)

    def test_guard_detects_nested_vendor_hook_and_modules_plant(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        nested_git = self.repo / "vendor" / "lib" / ".git"
        (nested_git / "hooks").mkdir(parents=True)
        (nested_git / "HEAD").write_text("ref: refs/heads/main\n")
        (nested_git / "config").write_text("[core]\n")
        (self.repo / ".git" / "modules" / "sub").mkdir(parents=True)
        (self.repo / ".git" / "modules" / "sub" / "HEAD").write_text(
            "ref: refs/heads/main\n"
        )
        (self.repo / ".git" / "modules" / "sub" / "hooks").mkdir(parents=True)
        baseline = capture_git_dir_guard(self.repo)
        (nested_git / "hooks" / "pre-commit").write_bytes(b"#!/bin/sh\necho evil\n")
        (nested_git / "HEAD").write_text("ref: refs/heads/evil\n")
        (self.repo / ".git" / "modules" / "sub" / "hooks" / "pre-commit").write_bytes(
            b"#!/bin/sh\necho modules\n"
        )
        after = capture_git_dir_guard(self.repo)
        changed = _changed_paths(baseline, after)
        self.assertIn("vendor/lib/.git/hooks/pre-commit", changed)
        self.assertIn("vendor/lib/.git/HEAD", changed)
        self.assertIn(".git/modules/sub/hooks/pre-commit", changed)


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
