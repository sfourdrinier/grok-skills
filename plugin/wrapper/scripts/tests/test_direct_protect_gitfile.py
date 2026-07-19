# wrapper/scripts/tests/test_direct_protect_gitfile.py
#
# In-workspace gitfile / modules/** discovery and pointer-redirect guard tests.

import os
import pathlib
import shutil
import stat
import tempfile
import unittest

from groklib.modes import direct_protect


class DirectProtectGitfileAndModulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="direct-protect-gitfile-")
        self.repo = pathlib.Path(self.tmp) / "repo"
        self.run_dir = pathlib.Path(self.tmp) / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        (self.repo / ".git" / "hooks").mkdir(parents=True)
        (self.repo / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

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

    def test_snapshot_persists_gitfile_prefix_map_survives_pointer_rewrite(self) -> None:
        # Snapshot must record prefix->actual gitdir for EVERY in-workspace
        # gitfile root. After snapshot, plant in original common and rewrite the
        # .git pointer external: restore must still hit the snapshotted common
        # (delete plant + restore HEAD), never claim restored while plant remains.
        common = self.repo / ".linked-common"
        (
            original_head,
            _cfg,
            original_hook,
            head,
            _config,
            hook,
        ) = self._seed_in_workspace_gitfile(self.repo / ".git", common)

        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn(".git", snap.git_roots)
        self.assertEqual(
            pathlib.Path(snap.git_roots[".git"]).resolve(), common.resolve()
        )

        planted = common / "hooks" / "post-commit"
        planted.write_bytes(b"#!/bin/sh\necho planted\n")
        head.write_text("ref: refs/heads/evil\n")
        hook.write_bytes(b"#!/bin/sh\necho evil\n")
        # Rewrite pointer outside workspace after baseline.
        (self.repo / ".git").write_text(
            "gitdir: /tmp/outside-common/.git/worktrees/x\n"
        )

        result = direct_protect.restore_protected_paths(
            self.repo,
            snap,
            offenders=[
                ".git/HEAD",
                ".git/hooks/pre-commit",
                ".git/hooks/post-commit",
            ],
        )
        self.assertEqual(head.read_text(), original_head)
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertFalse(
            planted.exists(),
            "planted hook must be deleted from ORIGINAL common, not claimed restored under external pointer",
        )
        for rel in (".git/HEAD", ".git/hooks/pre-commit", ".git/hooks/post-commit"):
            self.assertIn(rel, result.restored, result)
        self.assertEqual(result.unrestored, [])
        # Pointer bytes are outside auto-restore; rewritten pointer remains.
        self.assertIn("outside-common", (self.repo / ".git").read_text())

    def test_nested_gitfile_prefix_map_survives_pointer_rewrite(self) -> None:
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
        self.assertIn("vendor/lib/.git", snap.git_roots)
        self.assertEqual(
            pathlib.Path(snap.git_roots["vendor/lib/.git"]).resolve(),
            common.resolve(),
        )

        planted = common / "hooks" / "post-commit"
        planted.write_bytes(b"#!/bin/sh\necho plant\n")
        head.write_text("ref: refs/heads/evil\n")
        gitfile.write_text("gitdir: /tmp/outside-vendor-common\n")

        rel_head = "vendor/lib/.git/HEAD"
        rel_hook = "vendor/lib/.git/hooks/pre-commit"
        rel_plant = "vendor/lib/.git/hooks/post-commit"
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=[rel_head, rel_hook, rel_plant]
        )
        self.assertEqual(head.read_text(), original_head)
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertFalse(planted.exists())
        self.assertIn(rel_head, result.restored)
        self.assertIn(rel_hook, result.restored)
        self.assertIn(rel_plant, result.restored)
        self.assertEqual(result.unrestored, [])

    def test_modules_under_root_gitfile_target_inventoried(self) -> None:
        # modules/** under a gitfile common dir must use logical .git/modules/... keys.
        common = self.repo / ".linked-common"
        self._seed_in_workspace_gitfile(self.repo / ".git", common)
        mod = common / "modules" / "sub"
        (mod / "hooks").mkdir(parents=True)
        (mod / "HEAD").write_text("ref: refs/heads/main\n")
        (mod / "config").write_text("[core]\n")
        original_hook = b"#!/bin/sh\necho mod-good\n"
        (mod / "hooks" / "pre-commit").write_bytes(original_hook)
        os.chmod(str(mod / "hooks" / "pre-commit"), 0o755)

        roots = dict(direct_protect.discover_workspace_git_roots(self.repo))
        self.assertIn(".git/modules/sub", roots)
        self.assertEqual(
            pathlib.Path(roots[".git/modules/sub"]).resolve(), mod.resolve()
        )
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn(".git/modules/sub/HEAD", snap.entries)
        self.assertTrue(snap.entries[".git/modules/sub/HEAD"].snapshotted)
        self.assertIn(".git/modules/sub/hooks/pre-commit", snap.entries)
        (mod / "HEAD").write_text("ref: refs/heads/evil\n")
        planted = mod / "hooks" / "post-commit"
        planted.write_bytes(b"#!/bin/sh\necho plant\n")
        result = direct_protect.restore_protected_paths(
            self.repo,
            snap,
            offenders=[
                ".git/modules/sub/HEAD",
                ".git/modules/sub/hooks/pre-commit",
                ".git/modules/sub/hooks/post-commit",
            ],
        )
        self.assertEqual((mod / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((mod / "hooks" / "pre-commit").read_bytes(), original_hook)
        self.assertFalse(planted.exists())
        self.assertIn(".git/modules/sub/HEAD", result.restored)
        self.assertIn(".git/modules/sub/hooks/post-commit", result.restored)

    def test_modules_under_nested_freestanding_gitdir_inventoried(self) -> None:
        nested = self.repo / "vendor" / "lib" / ".git"
        (nested / "hooks").mkdir(parents=True)
        (nested / "HEAD").write_text("ref: refs/heads/main\n")
        (nested / "config").write_text("[core]\n")
        mod = nested / "modules" / "dep"
        (mod / "hooks").mkdir(parents=True)
        (mod / "HEAD").write_text("ref: refs/heads/main\n")
        (mod / "config").write_text("[core]\n")
        (mod / "hooks" / "pre-commit").write_bytes(b"#!/bin/sh\necho dep\n")
        roots = dict(direct_protect.discover_workspace_git_roots(self.repo))
        self.assertIn("vendor/lib/.git/modules/dep", roots)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn("vendor/lib/.git/modules/dep/HEAD", snap.entries)
        self.assertTrue(snap.entries["vendor/lib/.git/modules/dep/HEAD"].snapshotted)

    def test_modules_under_nested_gitfile_target_inventoried(self) -> None:
        common = self.repo / "vendor" / "lib" / ".actual-git"
        gitfile = self.repo / "vendor" / "lib" / ".git"
        self._seed_in_workspace_gitfile(gitfile, common)
        mod = common / "modules" / "dep"
        (mod / "hooks").mkdir(parents=True)
        (mod / "HEAD").write_text("ref: refs/heads/main\n")
        (mod / "config").write_text("[core]\n")
        roots = dict(direct_protect.discover_workspace_git_roots(self.repo))
        self.assertIn("vendor/lib/.git/modules/dep", roots)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        self.assertIn("vendor/lib/.git/modules/dep/config", snap.entries)

    def test_guard_union_detects_inworkspace_pointer_redirect_and_new_side_plant(
        self,
    ) -> None:
        # good -> evil in-workspace: after-guard must union baseline+live so the
        # new-side plant is detected, while restore still hits original common.
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        common = self.repo / ".linked-common"
        (
            original_head,
            _cfg,
            original_hook,
            head,
            _config,
            hook,
        ) = self._seed_in_workspace_gitfile(self.repo / ".git", common)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        baseline = capture_git_dir_guard(self.repo)

        evil = self.repo / ".evil-common"
        (evil / "hooks").mkdir(parents=True)
        (evil / "HEAD").write_text("ref: refs/heads/evil\n")
        (evil / "config").write_text("[core]\n")
        evil_plant = evil / "hooks" / "pre-commit"
        evil_plant.write_bytes(b"#!/bin/sh\necho evil-plant\n")
        (self.repo / ".git").write_text("gitdir: .evil-common\n")
        # Also rewrite original common HEAD so baseline side still flips.
        head.write_text("ref: refs/heads/moved\n")

        after = capture_git_dir_guard(self.repo, git_roots=snap.git_roots)
        changed = _changed_paths(baseline, after)
        self.assertIn(".git/HEAD", changed)
        self.assertIn(".git/hooks/pre-commit", changed)

        result = direct_protect.restore_protected_paths(
            self.repo,
            snap,
            offenders=sorted(changed),
        )
        # Original common restored via baseline map.
        self.assertEqual(head.read_text(), original_head)
        self.assertEqual(hook.read_bytes(), original_hook)
        # New-side plant must be deleted (live path) or honestly unrestored - not
        # claimed restored while still present on evil.
        if evil_plant.exists():
            self.assertIn(".git/hooks/pre-commit", result.unrestored)
        else:
            self.assertIn(".git/hooks/pre-commit", result.restored)

    def test_guard_detects_external_pointer_rewrite(self) -> None:
        from groklib.modes.direct_finalize import capture_git_dir_guard, _changed_paths

        common = self.repo / ".linked-common"
        self._seed_in_workspace_gitfile(self.repo / ".git", common)
        snap = direct_protect.snapshot_protected_paths(self.repo, self.run_dir)
        baseline = capture_git_dir_guard(self.repo)
        (self.repo / ".git").write_text(
            "gitdir: /tmp/outside-common/.git/worktrees/x\n"
        )
        after = capture_git_dir_guard(self.repo, git_roots=snap.git_roots)
        changed = _changed_paths(baseline, after)
        # Pointer rewrite must not be silent (gitfile content fingerprint).
        self.assertTrue(
            any(p == ".git" or p.endswith("/.git") for p in changed) or ".git" in changed,
            "external pointer rewrite must surface: {}".format(changed),
        )
        # Pointer bytes are outside auto-restore scope.
        result = direct_protect.restore_protected_paths(
            self.repo, snap, offenders=sorted(changed)
        )
        if ".git" in changed:
            self.assertNotIn(".git", result.restored)
            self.assertTrue(
                ".git" in result.unrestored
                or any(e.get("path") == ".git" for e in result.errors)
                or ".git" not in result.restored,
            )




if __name__ == "__main__":
    unittest.main()
