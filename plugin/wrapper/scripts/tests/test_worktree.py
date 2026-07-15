# wrapper/scripts/tests/test_worktree.py

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import Optional
from unittest import mock

from groklib import GrokWrapperError, runstate
from groklib.worktree import (
    ExternalWorktree,
    assert_committed_base_sufficient,
    capture_worktree_snapshot,
    create_external_worktree,
    diff_since_snapshot,
    diff_summary,
    remove_external_worktree,
    verify_external_worktree,
)
from groklib.worktree_escape import (
    assert_changes_within,
    assert_original_checkout_unmodified,
    capture_original_checkout_baseline,
    repo_change_fingerprint,
)

from tests import gitfixtures


def _git(repo: pathlib.Path, *args: str) -> str:
    argv = ["git", "-C", str(repo)] + [str(arg) for arg in args]
    completed = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        raise AssertionError(
            "test git helper failed: {} exit {} stderr={!r}".format(args, completed.returncode, completed.stderr)
        )
    return completed.stdout


class WorktreeTestBase(unittest.TestCase):
    """Isolates XDG_STATE_HOME (worktree root) and the fixture repo under tempfile."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-worktree-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        repo_parent = tempfile.mkdtemp(prefix="grok-cli-repo-", dir=self.tmp_root)
        self.repo_root = gitfixtures.make_repo(repo_parent)
        self.base = gitfixtures.head_revision(self.repo_root)

    def _create(self) -> ExternalWorktree:
        run_id = runstate.new_run_id()
        return create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)

    def _force_remove(self, wt: ExternalWorktree) -> None:
        # Test-only teardown that force-removes regardless of dirty state so the
        # temp repo's worktree registry is left clean; the tmp_root rmtree then
        # deletes everything on disk. Production removal (remove_external_worktree)
        # deliberately refuses dirty worktrees, so it cannot serve as teardown.
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(wt.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_root), "branch", "-D", wt.branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        if marker.exists():
            marker.unlink()

    def _worktree_list_paths(self) -> list:
        out = _git(self.repo_root, "worktree", "list", "--porcelain")
        paths = []
        for line in out.splitlines():
            if line.startswith("worktree "):
                paths.append(pathlib.Path(line[len("worktree "):]).resolve())
        return paths


class CreateWorktreeTests(WorktreeTestBase):
    def test_create_places_worktree_under_state_root_not_repo(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        worktrees_root = (runstate.state_root() / "worktrees").resolve()
        self.assertTrue(wt.path.resolve().is_relative_to(worktrees_root))
        self.assertFalse(wt.path.resolve().is_relative_to(self.repo_root.resolve()))
        self.assertTrue(wt.path.exists())
        # Sibling ownership marker lives NEXT TO the worktree, never inside it.
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        self.assertTrue(marker.exists())
        self.assertFalse((wt.path / "owner.json").exists())
        self.assertEqual(runstate.verify_owner_marker(marker), wt.path.name)

    def test_create_branch_name_is_grok_code_run_id(self) -> None:
        run_id = runstate.new_run_id()
        wt = create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)
        self.addCleanup(self._force_remove, wt)
        self.assertEqual(wt.branch, "grok/code/" + run_id)
        self.assertEqual(wt.path.name, run_id)
        self.assertEqual(wt.base_revision, self.base)

    def test_create_removes_orphan_worktree_and_branch_on_setup_failure(self) -> None:
        # Grok dogfood #6: any failure AFTER `git worktree add` (here the marker
        # write) must remove the just-added worktree AND its branch, leaving no
        # orphan the cleanup subcommand cannot safely adopt.
        run_id = runstate.new_run_id()
        branch = "grok/code/" + run_id

        def _boom(*args, **kwargs):
            raise OSError("simulated owner-marker write failure")

        with mock.patch.object(runstate, "write_owner_marker_file", _boom):
            with self.assertRaises(OSError):
                create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)

        # No worktree registered for this run id.
        self.assertFalse(
            any(path.name == run_id for path in self._worktree_list_paths()),
            "the orphaned worktree must be removed",
        )
        # The branch was force-deleted.
        branch_check = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--verify", "refs/heads/" + branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertNotEqual(branch_check.returncode, 0, "the orphaned branch must be deleted")

    def test_path_collision_fails_closed(self) -> None:
        run_id = runstate.new_run_id()
        wt = create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)
        self.addCleanup(self._force_remove, wt)
        with self.assertRaises(GrokWrapperError) as ctx:
            create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)
        self.assertEqual(ctx.exception.error_class, "worktree-failure")

    def test_branch_collision_fails_closed(self) -> None:
        run_id = runstate.new_run_id()
        # Pre-create the branch that create_external_worktree would want.
        _git(self.repo_root, "branch", "grok/code/" + run_id, self.base)
        with self.assertRaises(GrokWrapperError) as ctx:
            create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)
        self.assertEqual(ctx.exception.error_class, "worktree-failure")

    def test_unresolvable_base_fails_closed(self) -> None:
        run_id = runstate.new_run_id()
        with self.assertRaises(GrokWrapperError) as ctx:
            create_external_worktree(
                repo_root=self.repo_root, base="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", run_id=run_id
            )
        self.assertEqual(ctx.exception.error_class, "worktree-failure")

    def test_state_root_inside_checkout_rejected_before_nested_worktree(self) -> None:
        # PR968 codex state-root-in-checkout: an XDG_STATE_HOME under the target
        # checkout would derive the external worktree path INSIDE the real repo.
        # The guard must fail closed in preparation, before any nested `worktree
        # add`, leaving the operator's checkout clean (no nested worktree, no
        # tracked or untracked changes).
        inside_state = self.repo_root / ".grok-state"
        run_id = runstate.new_run_id()
        status_before = _git(self.repo_root, "status", "--porcelain")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(inside_state)}):
            with self.assertRaises(GrokWrapperError) as ctx:
                create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)
        self.assertEqual(ctx.exception.error_class, "worktree-failure")

        # No nested worktree was registered in the checkout.
        registered = [path.name for path in self._worktree_list_paths()]
        self.assertNotIn(run_id, registered, "no nested worktree may be created inside the checkout")
        # The checkout is left clean: no worktree add, no branch, nothing on disk.
        self.assertFalse((self.repo_root / ".grok-state").exists(), "no state dir may be created in the checkout")
        status_after = _git(self.repo_root, "status", "--porcelain")
        self.assertEqual(status_after, status_before, "the guard must add no new changes to the operator checkout")
        branch_check = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--verify", "refs/heads/grok/code/" + run_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertNotEqual(branch_check.returncode, 0, "no worktree branch may be created")


class VerifyWorktreeTests(WorktreeTestBase):
    def test_verify_passes_for_created_worktree_and_fails_for_repo_subdir(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        # Passes for the real external worktree.
        verify_external_worktree(wt)

        # A registered worktree INSIDE the repo root must be rejected by the
        # containment guard even though it appears in `worktree list`.
        inside_path = self.repo_root / "inside-wt"
        _git(self.repo_root, "worktree", "add", "-b", "inside-branch", str(inside_path), self.base)
        self.addCleanup(_git, self.repo_root, "worktree", "remove", "--force", str(inside_path))
        inside_wt = ExternalWorktree(
            path=inside_path, branch="inside-branch", base_revision=self.base, repo_root=self.repo_root
        )
        with self.assertRaises(GrokWrapperError) as ctx:
            verify_external_worktree(inside_wt)
        self.assertEqual(ctx.exception.error_class, "worktree-failure")

    def test_verify_fails_when_not_registered(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        phantom = ExternalWorktree(
            path=runstate.state_root() / "worktrees" / "repo" / "never-registered",
            branch="grok/code/never-registered",
            base_revision=self.base,
            repo_root=self.repo_root,
        )
        with self.assertRaises(GrokWrapperError) as ctx:
            verify_external_worktree(phantom)
        self.assertEqual(ctx.exception.error_class, "worktree-failure")


class DiffSummaryTests(WorktreeTestBase):
    def test_diff_summary_lists_tracked_and_untracked_changes(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        with (wt.path / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("worktree-edit\n")
        (wt.path / "brand_new.txt").write_text("new content\n", encoding="utf-8")

        changed, stat_text = diff_summary(wt)
        self.assertIn("a.txt", changed)
        self.assertIn("brand_new.txt", changed)
        self.assertIn("a.txt", stat_text)


class VerifySnapshotArtifactRuleTests(WorktreeTestBase):
    """The verify entry/exit snapshot + artifact-tolerance combined rule (PR968 codex).

    Exercises the FULL verify surface: capture_worktree_snapshot (entry) -> run write ->
    diff_since_snapshot -> assert_changes_within with verify's EMPTY allowed-roots. The
    snapshots stage with ``git add -A -f``, so a gitignored write IS captured in the diff;
    a changed path is tolerated ONLY when it is BOTH gitignored AND under a build-artifact
    dir. A gitignored NON-artifact (e.g. .env.local) and a tracked build-dir source are
    flagged; a gitignored dist/ or node_modules/ build output is tolerated.
    """

    def _worktree_with_gitignore(self, *, extra_tracked: Optional[dict] = None) -> ExternalWorktree:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        (wt.path / ".gitignore").write_text("dist/\nnode_modules/\n*.local\n", encoding="utf-8")
        # Optional tracked files committed so a later edit is a TRACKED (not ignored)
        # change; .gitignore is committed too so it is unchanged between the snapshots.
        if extra_tracked:
            for relative, contents in extra_tracked.items():
                target = wt.path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
        _git(wt.path, "add", "-A")
        _git(wt.path, "commit", "-q", "-m", "seed .gitignore and tracked files")
        return wt

    def test_verify_flags_gitignored_non_artifact_write(self) -> None:
        # A verify command writing a gitignored NON-artifact (.env.local) must be
        # FLAGGED: the -f snapshot captures it, and it is not under a build-artifact dir
        # so the artifact tolerance never applies. Without the -f fix the write would be
        # invisible to the diff and slip the gate entirely.
        wt = self._worktree_with_gitignore()
        baseline = capture_original_checkout_baseline(self.repo_root)
        entry = capture_worktree_snapshot(wt)

        (wt.path / ".env.local").write_text("SECRET=leaked\n", encoding="utf-8")
        changed, _stat = diff_since_snapshot(wt, entry)
        self.assertIn(".env.local", changed, "the gitignored write must be captured by the -f snapshot")

        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(
                wt, (), worktree_changed=changed, original_baseline=baseline
            )
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any(".env.local" in v for v in ctx.exception.detail["violations"]))

    def test_verify_flags_tracked_build_dir_source_edit(self) -> None:
        # A TRACKED file under a build/ dir that is NOT gitignored, modified during verify,
        # must be FLAGGED (preserve batch-3): the "build" component alone never tolerates;
        # only a genuinely gitignored artifact path does.
        wt = self._worktree_with_gitignore(extra_tracked={"build/orchestrate.ts": "export const v = 1\n"})
        baseline = capture_original_checkout_baseline(self.repo_root)
        entry = capture_worktree_snapshot(wt)

        with (wt.path / "build" / "orchestrate.ts").open("a", encoding="utf-8") as handle:
            handle.write("// verify-run edit\n")
        changed, _stat = diff_since_snapshot(wt, entry)
        self.assertIn("build/orchestrate.ts", changed)

        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(
                wt, (), worktree_changed=changed, original_baseline=baseline
            )
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("build/orchestrate.ts" in v for v in ctx.exception.detail["violations"]))

    def test_verify_tolerates_gitignored_dist_build_output(self) -> None:
        # A genuinely gitignored dist/ build output written during verify is a legitimate
        # build artifact and must be TOLERATED (preserve legitimate builds).
        wt = self._worktree_with_gitignore()
        baseline = capture_original_checkout_baseline(self.repo_root)
        entry = capture_worktree_snapshot(wt)

        (wt.path / "dist").mkdir()
        (wt.path / "dist" / "index.js").write_text("// built\n", encoding="utf-8")
        changed, _stat = diff_since_snapshot(wt, entry)
        self.assertIn("dist/index.js", changed, "the -f snapshot still captures the ignored build output")

        # No raise: gitignored AND under a build-artifact dir -> tolerated.
        assert_changes_within(wt, (), worktree_changed=changed, original_baseline=baseline)

    def test_verify_tolerates_gitignored_node_modules_write(self) -> None:
        # A gitignored node_modules/ write (a dependency install during a build gate) is a
        # disposable build artifact and must be TOLERATED.
        wt = self._worktree_with_gitignore()
        baseline = capture_original_checkout_baseline(self.repo_root)
        entry = capture_worktree_snapshot(wt)

        (wt.path / "node_modules" / "left-pad").mkdir(parents=True)
        (wt.path / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
        changed, _stat = diff_since_snapshot(wt, entry)
        self.assertIn("node_modules/left-pad/index.js", changed)

        assert_changes_within(wt, (), worktree_changed=changed, original_baseline=baseline)


class RepoChangeFingerprintTests(WorktreeTestBase):
    def test_fingerprint_detects_rewrite_of_already_dirty_file(self) -> None:
        # Grok dogfood-4 #2 review-fs-content: a rewrite of an ALREADY-dirty file
        # changes its content fingerprint, so the before/after set difference is
        # non-empty even though the PATH set is unchanged (a path-only diff missed it).
        dirty = self.repo_root / "a.txt"
        with dirty.open("a", encoding="utf-8") as handle:
            handle.write("operator's own edit\n")
        before = repo_change_fingerprint(self.repo_root)
        self.assertIn("a.txt", {path for path, _fp in before})

        # A path-only diff would see the same {a.txt, dirty.txt} set before/after.
        with dirty.open("a", encoding="utf-8") as handle:
            handle.write("a run-attributable rewrite\n")
        after = repo_change_fingerprint(self.repo_root)
        changed = {path for path, _fp in (after - before)}
        self.assertIn("a.txt", changed, "a rewrite of an already-dirty file must be detected")
        self.assertNotIn("dirty.txt", changed, "the operator's untouched pre-existing dirt is not flagged")


class AssertChangesWithinTests(WorktreeTestBase):
    def test_assert_changes_within_flags_outside_writes(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        # Production always passes an entry baseline (pre-existing tracked + untracked
        # dirt), so the operator's untracked dirty.txt is exempt while a run write is
        # flagged. Capture it before any escape.
        baseline = capture_original_checkout_baseline(self.repo_root)

        # In-worktree change confined to the worktree passes.
        (wt.path / "pkg" / "generated.txt").write_text("ok\n", encoding="utf-8")
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)

        # A write to a TRACKED file in the ORIGINAL checkout (Grok editing real
        # source) must be flagged, while the pre-existing untracked dirty.txt is
        # tolerated (it is the operator's, not run-introduced).
        with (self.repo_root / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("escaped-write\n")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_assert_changes_within_flags_planted_untracked_file_in_original_checkout(self) -> None:
        # original-checkout-scan-misses-untracked-new-files: a sandbox bypass that
        # PLANTS a brand-new untracked file into the operator's real checkout (not a
        # tracked edit) must be flagged; a path-only tracked-diff scan missed it.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)

        # Pre-existing operator dirt (dirty.txt) stays exempt; only the newly
        # planted file is a violation.
        (self.repo_root / "exfil.env").write_text("SECRET=planted\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("exfil.env" in v for v in ctx.exception.detail["violations"]))
        self.assertFalse(any("dirty.txt" in v for v in ctx.exception.detail["violations"]))

    def test_worktree_change_outside_allowed_subroot_flagged(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)
        # Change lands at the worktree root, but only pkg/ is allowed.
        (wt.path / "outside.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path / "pkg",), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_assert_changes_within_tolerates_gitignored_build_artifacts(self) -> None:
        # Grok dogfood #7: a multi-package workspace build writes into per-package
        # packages/<pkg>/dist and .next, NESTED below the worktree root. These
        # are tolerated ONLY when git genuinely ignores them (a real disposable
        # build root), not merely because a same-named component appears.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        baseline = capture_original_checkout_baseline(self.repo_root)
        (wt.path / ".gitignore").write_text("dist/\n.next/\n", encoding="utf-8")
        nested_dist = wt.path / "packages" / "foo" / "dist"
        nested_dist.mkdir(parents=True)
        (nested_dist / "bundle.js").write_text("// built\n", encoding="utf-8")
        nested_next = wt.path / "packages" / "foo" / ".next"
        nested_next.mkdir(parents=True)
        (nested_next / "manifest.json").write_text("{}\n", encoding="utf-8")

        # The gitignored artifact paths are tolerated even with a narrow root; the
        # operator's pre-existing untracked dirt is exempted by the entry baseline.
        assert_changes_within(
            wt,
            (wt.path / "pkg",),
            worktree_changed=[
                "packages/foo/dist/bundle.js",
                "packages/foo/.next/manifest.json",
            ],
            original_baseline=baseline,
        )

    def test_assert_changes_within_flags_dist_under_src_not_ignored(self) -> None:
        # Grok dogfood #5 attack: writing to packages/foo/src/dist/backdoor.ts
        # carries a "dist" component but is NOT gitignored -- it is a source-tree
        # escape and must still be flagged closed, never exempted by name.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        baseline = capture_original_checkout_baseline(self.repo_root)
        # Only dist/ and .next/ at any level are ignored; src/dist is NOT.
        (wt.path / ".gitignore").write_text("/dist/\n", encoding="utf-8")
        backdoor = wt.path / "packages" / "foo" / "src" / "dist"
        backdoor.mkdir(parents=True)
        (backdoor / "backdoor.ts").write_text("export const pwn = 1\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(
                wt,
                (wt.path / "pkg",),
                worktree_changed=["packages/foo/src/dist/backdoor.ts"],
                original_baseline=baseline,
            )
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

        # A plain nested SOURCE edit (no artifact component) is also flagged.
        nested_src = wt.path / "packages" / "foo" / "src"
        (nested_src / "code.ts").write_text("export const x = 1\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(
                wt, (wt.path / "pkg",), worktree_changed=["packages/foo/src/code.ts"], original_baseline=baseline
            )
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_pre_dirty_original_file_rewritten_during_run_is_flagged(self) -> None:
        # Round5 original-checkout-baseline-path-only-exclusion: a tracked file
        # already dirty at run entry must be exempt ONLY while its content is
        # unchanged. If the run REWRITES it (an escape), it must be flagged, not
        # permanently blessed by a path-only exemption.
        pre_dirty = self.repo_root / "a.txt"
        with pre_dirty.open("a", encoding="utf-8") as handle:
            handle.write("operator's own pre-existing edit\n")
        # Baseline captured at run entry, with a.txt already dirty.
        baseline = capture_original_checkout_baseline(self.repo_root)
        self.assertIn("a.txt", baseline)

        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        # Unchanged since entry -> still exempt (no false positive on operator dirt).
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)

        # The run now REWRITES the already-dirty file in the REAL checkout.
        with pre_dirty.open("a", encoding="utf-8") as handle:
            handle.write("// run-attributable escape into an already-dirty file\n")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("a.txt" in v for v in ctx.exception.detail["violations"]))

    def test_mode_only_change_to_pre_dirty_original_file_is_flagged(self) -> None:
        # Round7 mode-only-change-baseline-exemption-bypass: a tracked file already
        # dirty at run entry must be exempt only while BOTH its content and its mode
        # are unchanged. A mode-only mutation (chmod +x -- content byte-identical, so
        # git hash-object is unchanged) is still a run-attributable change and must be
        # flagged, not silently blessed as pre-existing operator dirt.
        pre_dirty = self.repo_root / "a.txt"
        os.chmod(str(pre_dirty), 0o644)
        with pre_dirty.open("a", encoding="utf-8") as handle:
            handle.write("operator's own pre-existing edit\n")
        baseline = capture_original_checkout_baseline(self.repo_root)
        self.assertIn("a.txt", baseline)

        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        # Unchanged content AND mode since entry -> still exempt.
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)

        # A MODE-ONLY mutation with NO content change (chmod +x).
        os.chmod(str(pre_dirty), 0o755)
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("a.txt" in v for v in ctx.exception.detail["violations"]))

    def test_assert_changes_within_flags_planted_gitignored_file_in_original_checkout(self) -> None:
        # Grok r5 #4: the original-checkout escape scan must flag ANY modification
        # including gitignored files -- a sandbox bypass that PLANTS a gitignored file
        # (repo/.env.local, repo/node_modules/.bin/evil) in the operator's real
        # checkout was invisible to the --exclude-standard scan. The .gitignore is
        # captured into the baseline first (exempt operator dirt); only the newly
        # planted ignored file is a violation.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        (self.repo_root / ".gitignore").write_text("secret/\n*.local\n", encoding="utf-8")
        baseline = capture_original_checkout_baseline(self.repo_root)

        # Plant a gitignored file deep inside an ignored directory AND at the root.
        (self.repo_root / "secret").mkdir()
        (self.repo_root / "secret" / "evil.bin").write_text("exfil\n", encoding="utf-8")
        (self.repo_root / "creds.local").write_text("SECRET=planted\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        violations = ctx.exception.detail["violations"]
        self.assertTrue(any("secret/evil.bin" in v for v in violations), violations)
        self.assertTrue(any("creds.local" in v for v in violations), violations)
        # Pre-existing operator dirt (dirty.txt) stays exempt.
        self.assertFalse(any("dirty.txt" in v for v in violations))

    def test_original_checkout_artifact_named_edit_always_flagged(self) -> None:
        # Escape detection of edits in the operator's REAL checkout must NEVER be
        # softened by artifact tolerance: a tracked file under a "build"-named
        # directory, newly diverged during the run, is still an escape.
        artifact_dir = self.repo_root / "build"
        artifact_dir.mkdir()
        tracked = artifact_dir / "orchestrate.ts"
        tracked.write_text("export const v = 1\n", encoding="utf-8")
        _git(self.repo_root, "add", "-A")
        _git(self.repo_root, "commit", "-q", "-m", "add tracked build file")
        base = _git(self.repo_root, "rev-parse", "HEAD").strip()

        run_id = runstate.new_run_id()
        wt = create_external_worktree(repo_root=self.repo_root, base=base, run_id=run_id)
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)

        # Grok "escapes" and edits the tracked build file in the REAL checkout.
        with tracked.open("a", encoding="utf-8") as handle:
            handle.write("// escaped edit\n")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_original_checkout_unmodified_flags_post_gate_write_but_not_worktree_output(self) -> None:
        # PR968 codex post-build-gate: the re-scan run AFTER the build gate must flag a
        # write into the operator's REAL checkout (a Grok-modified build script escaping)
        # while ignoring the isolated worktree's OWN build outputs, so a legitimate gate
        # that only writes inside the worktree never false-positives.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)

        # A legitimate build writes only inside the worktree (its own outputs) -- the
        # original-checkout re-scan must ignore it entirely.
        (wt.path / "pkg" / "built.js").write_text("// built\n", encoding="utf-8")
        assert_original_checkout_unmodified(wt, (wt.path,), original_baseline=baseline)

        # A Grok-modified build step escaping into the REAL checkout is flagged, while
        # the operator's pre-existing untracked dirty.txt stays exempt.
        (self.repo_root / "gate-escaped.txt").write_text("planted by build\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_original_checkout_unmodified(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertEqual(ctx.exception.detail.get("phase"), "post-build-gate")
        self.assertTrue(any("gate-escaped.txt" in v for v in ctx.exception.detail["violations"]))
        self.assertFalse(any("dirty.txt" in v for v in ctx.exception.detail["violations"]))


class CommittedBaseTests(WorktreeTestBase):
    def test_committed_base_sufficient_for_committed_path(self) -> None:
        assert_committed_base_sufficient(self.repo_root, self.base, ("pkg/mod.txt", "a.txt"))

    def test_committed_base_insufficient_when_path_uncommitted(self) -> None:
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_committed_base_sufficient(self.repo_root, self.base, ("dirty.txt",))
        self.assertEqual(ctx.exception.error_class, "worktree-failure")

    def test_committed_base_rejects_non_ancestor(self) -> None:
        # Create a commit on a side branch that is NOT an ancestor of HEAD.
        _git(self.repo_root, "branch", "side", self.base)
        _git(self.repo_root, "worktree", "add", str(pathlib.Path(self.tmp_root) / "sidewt"), "side")
        side_wt = pathlib.Path(self.tmp_root) / "sidewt"
        self.addCleanup(_git, self.repo_root, "worktree", "remove", "--force", str(side_wt))
        (side_wt / "side.txt").write_text("s\n", encoding="utf-8")
        _git(side_wt, "add", "-A")
        _git(side_wt, "commit", "-q", "-m", "side commit")
        side_sha = _git(side_wt, "rev-parse", "HEAD").strip()
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_committed_base_sufficient(self.repo_root, side_sha, ("a.txt",))
        self.assertEqual(ctx.exception.error_class, "worktree-failure")


class RemoveWorktreeTests(WorktreeTestBase):
    def test_remove_dry_run_reports_without_removing(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        report = remove_external_worktree(wt, confirmed=False, expected_run_id=wt.path.name)
        self.assertFalse(report["removed"])
        self.assertEqual(report["worktreePath"], str(wt.path))
        self.assertEqual(report["worktreeBranch"], wt.branch)
        self.assertTrue(wt.path.exists())
        self.assertIn(wt.path.resolve(), self._worktree_list_paths())

    def test_remove_confirmed_removes_dirty_owner_marked_worktree(self) -> None:
        # Grok dogfood-2 #8: code mode leaves its worktree dirty by design, so a
        # confirmed cleanup of an OWNER-MARKED worktree must remove it (--force),
        # not refuse it -- the marker + --confirm are the authority.
        wt = self._create()
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        (wt.path / "dirty_in_worktree.txt").write_text("uncommitted\n", encoding="utf-8")
        report = remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertTrue(report["removed"])
        self.assertTrue(report["dirty"], "the dirty state is still reported honestly")
        self.assertFalse(wt.path.exists())
        self.assertFalse(marker.exists())
        self.assertNotIn(wt.path.resolve(), self._worktree_list_paths())

    def test_partial_rollback_records_marker_when_removal_fails(self) -> None:
        # Round4 F3-worktree-orphan: when the rollback's worktree removal cannot
        # complete, the worktree/branch must NOT be left registered with no marker.
        # A valid owner marker is written so cleanup can reap it later.
        from groklib import worktree as wt_mod

        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        # Simulate the partial-setup state: no marker written yet.
        if marker.exists():
            marker.unlink()

        real_query = wt_mod._git_query

        def _query(repo, *args):
            if args[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(list(args), 1, "", "simulated worktree lock")
            return real_query(repo, *args)

        with mock.patch.object(wt_mod, "_git_query", _query):
            wt_mod._remove_partial_worktree(wt.repo_root, wt.path, wt.branch, marker, wt.path.name)

        self.assertTrue(marker.exists(), "an unremovable worktree must be recorded with an owner marker")
        self.assertEqual(runstate.verify_owner_marker(marker), wt.path.name)
        self.assertTrue(wt.path.exists())

    def test_stranded_create_failure_is_reapable_by_cleanup(self) -> None:
        # PR968 codex record-partial-worktree: a create failure whose rollback
        # CANNOT remove the just-added worktree must annotate the raised error with
        # the stranded worktree identity, so the caller records it into run.json and
        # a later cleanup CAN remove the stranded worktree + grok/code/<run> branch.
        from groklib import worktree as wt_mod

        run_id = runstate.new_run_id()
        branch = "grok/code/" + run_id
        real_write = runstate.write_owner_marker_file
        real_query = wt_mod._git_query
        write_calls = {"n": 0}

        def _write(marker_path, marker_run_id):
            # Fail the SETUP marker write (first call) to trigger rollback; let the
            # strand-branch marker write (second call) succeed so the worktree is
            # marker-recorded and reapable, exactly as in production.
            write_calls["n"] += 1
            if write_calls["n"] == 1:
                raise OSError("simulated setup owner-marker write failure")
            return real_write(marker_path, marker_run_id)

        def _query(repo, *args):
            if args[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(list(args), 1, "", "simulated worktree lock")
            return real_query(repo, *args)

        with mock.patch.object(runstate, "write_owner_marker_file", _write), \
                mock.patch.object(wt_mod, "_git_query", _query):
            with self.assertRaises(OSError) as ctx:
                create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=run_id)

        stranded = wt_mod.stranded_worktree_from_error(ctx.exception)
        self.assertIsNotNone(stranded, "the stranded worktree must be recorded on the raised error")
        self.assertEqual(stranded.path.name, run_id)
        self.assertEqual(stranded.branch, branch)
        self.addCleanup(self._force_remove, stranded)

        # The worktree + branch survived the failed rollback, marker-recorded.
        self.assertTrue(stranded.path.exists())
        marker = pathlib.Path(str(stranded.path) + ".owner.json")
        self.assertEqual(runstate.verify_owner_marker(marker), run_id)
        branch_before = subprocess.run(
            ["git", "-C", str(self.repo_root), "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
            check=False,
        )
        self.assertEqual(branch_before.returncode, 0, "the grok/code/<run> branch survived the failed rollback")

        # Cleanup (unpatched removal) reaps the stranded worktree AND its branch.
        report = remove_external_worktree(stranded, confirmed=True, expected_run_id=run_id)
        self.assertTrue(report["removed"])
        self.assertFalse(report["branchRetained"], "the run-bound branch is deleted, not retained")
        self.assertFalse(stranded.path.exists())
        self.assertFalse(marker.exists())
        self.assertNotIn(stranded.path.resolve(), self._worktree_list_paths())
        branch_after = subprocess.run(
            ["git", "-C", str(self.repo_root), "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
            check=False,
        )
        self.assertNotEqual(branch_after.returncode, 0, "the stranded branch must be reaped by cleanup")

    def test_remove_confirmed_removes_worktree_and_branch(self) -> None:
        wt = self._create()
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        report = remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertTrue(report["removed"])
        self.assertFalse(wt.path.exists())
        self.assertFalse(marker.exists())
        self.assertNotIn(wt.path.resolve(), self._worktree_list_paths())
        branch_check = subprocess.run(
            ["git", "-C", str(self.repo_root), "show-ref", "--verify", "--quiet", "refs/heads/" + wt.branch],
            check=False,
        )
        self.assertNotEqual(branch_check.returncode, 0)

    def test_remove_confirmed_when_worktree_path_already_missing_reaps_marker_and_branch(self) -> None:
        # Grok dogfood-4 #1 cleanup-wedge: the worktree directory is already gone
        # (operator rm, or a crash after `git worktree remove` before the run-dir
        # delete) but the verified sibling marker remains. This must be treated as
        # ALREADY-REMOVED (removed=True, branch+marker reaped) so cleanup can then
        # delete runs/<id>/, NOT raise worktree-failure and wedge forever.
        wt = self._create()
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        # Remove ONLY the worktree directory + its git registration; the sibling
        # marker (and, in production, the run dir) still exist.
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(wt.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertFalse(wt.path.exists())
        self.assertTrue(marker.exists(), "the sibling marker still exists")

        report = remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertTrue(report["removed"])
        self.assertTrue(report["worktreeMissing"])
        self.assertFalse(marker.exists(), "the sibling marker is reaped")
        self.assertNotIn(wt.path.resolve(), self._worktree_list_paths())

    def test_remove_dry_run_reports_worktree_missing_without_removing(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(wt.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        report = remove_external_worktree(wt, confirmed=False, expected_run_id=wt.path.name)
        self.assertFalse(report["removed"])
        self.assertTrue(report["worktreeMissing"])

    def test_remove_confirmed_retains_branch_with_unmerged_commits_without_wedging(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        marker = pathlib.Path(str(wt.path) + ".owner.json")

        # Commit inside the worktree so the branch has a commit that is not on
        # the main repo's HEAD; the worktree itself is otherwise clean
        # (committed, not dirty) so the dirty-refusal guard does not apply.
        (wt.path / "unmerged.txt").write_text("grok wrote this\n", encoding="utf-8")
        _git(wt.path, "add", "-A")
        _git(wt.path, "commit", "-q", "-m", "commit made inside the worktree")

        report = remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)

        self.assertTrue(report["removed"])
        self.assertTrue(report["branchRetained"])
        self.assertTrue(report["branchRetainReason"])
        self.assertFalse(wt.path.exists())
        self.assertFalse(marker.exists())
        self.assertNotIn(wt.path.resolve(), self._worktree_list_paths())

        branch_list = _git(self.repo_root, "branch", "--list", wt.branch)
        self.assertIn(wt.branch, branch_list)

    def test_remove_confirmed_retry_after_full_reap_is_idempotent(self) -> None:
        # PR968 codex cleanup-retryable: a first confirmed removal reaps the worktree,
        # its sibling marker, AND its branch. If a LATER step fails (the caller's run-dir
        # delete) and cleanup is retried, this second call must NOT raise on the now-absent
        # sibling marker (which would wedge the run dir forever) -- it treats the fully
        # reaped worktree as already-removed and returns removed=True so the caller can
        # finish deleting the run dir.
        wt = self._create()
        marker = pathlib.Path(str(wt.path) + ".owner.json")

        first = remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertTrue(first["removed"])
        self.assertFalse(wt.path.exists())
        self.assertFalse(marker.exists())

        # Retry with the SAME (now stale) worktree record: marker + dir + branch all gone.
        second = remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertTrue(second["removed"], "the retry must complete, not wedge on the absent marker")
        self.assertTrue(second["worktreeMissing"])
        # The branch was already deleted in the first pass; the retry must not falsely
        # claim it was retained.
        self.assertFalse(second["branchRetained"])

    def test_remove_confirmed_retry_refuses_stale_record_for_foreign_run(self) -> None:
        # The already-reaped retry path still enforces the path-name binding: a stale
        # record whose worktree path name is NOT the requested run id is refused even
        # when both dir and marker are absent, so it can never smuggle through.
        wt = self._create()
        remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertFalse(wt.path.exists())

        other_run_id = runstate.new_run_id()
        self.assertNotEqual(other_run_id, wt.path.name)
        with self.assertRaises(GrokWrapperError) as ctx:
            remove_external_worktree(wt, confirmed=True, expected_run_id=other_run_id)
        self.assertEqual(ctx.exception.error_class, "state-ownership-violation")

    def test_remove_wrong_owner_marker_fails_closed(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        marker.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "owner": "some-other-tool",
                    "runId": wt.path.name,
                    "createdAtUtc": "2026-07-14T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(GrokWrapperError) as ctx:
            remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)
        self.assertEqual(ctx.exception.error_class, "state-ownership-violation")
        self.assertTrue(wt.path.exists())

    def test_remove_refuses_when_expected_run_id_differs_from_worktree(self) -> None:
        # PR968 codex #4: a stale/corrupt run.json for run A can point at run B's
        # worktree, whose OWN marker (id B == dir name B) would satisfy a
        # wt.path.name-only check. Binding to the REQUESTED run id (A) refuses the
        # destructive removal so run B's worktree, marker, and branch survive.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        marker = pathlib.Path(str(wt.path) + ".owner.json")
        other_run_id = runstate.new_run_id()
        self.assertNotEqual(other_run_id, wt.path.name)

        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                with self.assertRaises(GrokWrapperError) as ctx:
                    remove_external_worktree(wt, confirmed=confirmed, expected_run_id=other_run_id)
                self.assertEqual(ctx.exception.error_class, "state-ownership-violation")
                self.assertEqual(ctx.exception.detail["requestedRunId"], other_run_id)
                self.assertEqual(ctx.exception.detail["worktreeRunId"], wt.path.name)
                # Nothing about run B's worktree is touched.
                self.assertTrue(wt.path.exists())
                self.assertTrue(marker.exists())
                self.assertIn(wt.path.resolve(), self._worktree_list_paths())

    def test_remove_does_not_delete_unrelated_recorded_branch(self) -> None:
        # PR968 codex bind-branch-deletion: a stale/corrupt run.json can pair the
        # correct owner-marked worktree path with an UNRELATED worktreeBranch (e.g. a
        # merged feature branch). Cleanup must NOT delete that branch -- only the
        # run-bound grok/code/<run_id> is ever removed. Fail closed.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        run_id = wt.path.name

        unrelated_branch = "feature/merged-work"
        _git(self.repo_root, "branch", unrelated_branch, self.base)

        # A record whose worktreeBranch points at the unrelated branch, not this
        # worktree's real grok/code/<run> branch.
        tampered = ExternalWorktree(
            path=wt.path,
            branch=unrelated_branch,
            base_revision=wt.base_revision,
            repo_root=wt.repo_root,
        )

        report = remove_external_worktree(tampered, confirmed=True, expected_run_id=run_id)

        # The worktree itself is bound by path/marker, so it is removed.
        self.assertTrue(report["removed"])
        self.assertFalse(wt.path.exists())
        # The unrelated branch is refused (retained + reported), never deleted.
        self.assertTrue(report["branchRetained"])
        self.assertIsNotNone(report["branchRetainReason"])
        unrelated_check = subprocess.run(
            ["git", "-C", str(self.repo_root), "show-ref", "--verify", "--quiet", "refs/heads/" + unrelated_branch],
            check=False,
        )
        self.assertEqual(unrelated_check.returncode, 0, "the unrelated branch must NOT be deleted")
        _git(self.repo_root, "branch", "-D", unrelated_branch)


class LifecycleIsolationTests(WorktreeTestBase):
    def test_original_checkout_dirty_file_untouched_through_full_lifecycle(self) -> None:
        dirty_path = self.repo_root / "dirty.txt"
        before_content = dirty_path.read_text(encoding="utf-8")
        before_mtime = dirty_path.stat().st_mtime_ns

        wt = self._create()
        # Production passes the entry baseline so the operator's pre-existing
        # untracked dirty.txt is exempt from the original-checkout scan.
        baseline = capture_original_checkout_baseline(self.repo_root)
        (wt.path / "a.txt").write_text("changed in worktree\n", encoding="utf-8")
        diff_summary(wt)
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        # Reset the worktree so it is clean enough for a no-force removal.
        _git(wt.path, "checkout", "--", "a.txt")
        remove_external_worktree(wt, confirmed=True, expected_run_id=wt.path.name)

        self.assertEqual(dirty_path.read_text(encoding="utf-8"), before_content)
        self.assertEqual(dirty_path.stat().st_mtime_ns, before_mtime)


if __name__ == "__main__":
    unittest.main()
