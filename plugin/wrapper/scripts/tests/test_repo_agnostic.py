# wrapper/scripts/tests/test_repo_agnostic.py
#
# Proves the STANDALONE, repo-agnostic contract: the repository root is derived
# from the RESOLVED --target (git toplevel), NEVER from where this wrapper is
# installed. A --target pointing at ANY repo on disk -- one that does NOT contain
# the wrapper and is NOT the caller's cwd -- resolves to THAT repo and confines
# every guard there. These tests deliberately do NOT chdir into the target repo,
# so a cwd-derived or wrapper-location-derived root would resolve to the wrong
# place (or fail) and the assertions would catch it.

import json
import pathlib
import shutil
import tempfile
import unittest

from groklib import GrokWrapperError
from groklib.modes import _shared

from tests import gitfixtures
from tests.modefixtures import ModeHarness, make_review_repo
from tests.worktreefixtures import WorktreeModeHarness


def _plant_sentinel(worktree_path: pathlib.Path, run_id: str) -> None:
    (worktree_path / (".grok-run-" + run_id)).write_text("", encoding="utf-8")


class RepoRootDerivationUnitTests(unittest.TestCase):
    """repo_root_for_path derives the git toplevel that CONTAINS the anchor, not the wrapper location."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-agnostic-unit-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)

    def test_root_is_derived_from_the_target_not_the_wrapper(self) -> None:
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        # A subdirectory anchor still resolves to the repo root of THAT repo.
        derived = _shared.repo_root_for_path(repo / "pkg")
        self.assertEqual(derived.resolve(), repo.resolve())

        # The derived root is the TARGET's repo, provably not the grok-skills tree
        # this wrapper lives in.
        wrapper_tree = pathlib.Path(__file__).resolve()
        self.assertNotIn(str(repo.resolve()), str(wrapper_tree))

    def test_file_anchor_resolves_to_its_repo_root(self) -> None:
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        derived = _shared.repo_root_for_path(repo / "pkg" / "module.txt")
        self.assertEqual(derived.resolve(), repo.resolve())

    def test_target_outside_any_git_repo_fails_closed(self) -> None:
        loose_dir = pathlib.Path(self.tmp_root) / "not-a-repo"
        loose_dir.mkdir()
        with self.assertRaises(GrokWrapperError) as caught:
            _shared.repo_root_for_path(loose_dir)
        self.assertEqual(caught.exception.error_class, "invalid-target")


class ReviewExternalTargetTests(ModeHarness):
    """review confines to the repo that CONTAINS an absolute --target, with no chdir into it."""

    def test_absolute_target_outside_wrapper_resolves_to_that_repo(self) -> None:
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        # repo_root=None: the harness does NOT chdir into the repo, so the ONLY way
        # the run can resolve the right root is by deriving it from the absolute
        # --target. A wrapper-location or cwd derivation would resolve elsewhere.
        exit_code, out = self.drive(
            ["review", "--target", str(repo / "pkg"), "--task", "Review"],
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")
        # The envelope's repository is the EXTERNAL target repo, not grok-skills.
        self.assertEqual(pathlib.Path(env["repository"]).resolve(), repo.resolve())
        self.assertEqual(env["targetWorkspace"], "pkg")
        # And the run cwd is inside that external repo.
        argv = self.read_run_argv()
        cwd = pathlib.Path(self.flag_value(argv, "--cwd")).resolve()
        self.assertEqual(cwd, (repo / "pkg").resolve())

    def test_absolute_target_not_in_any_repo_is_invalid_target(self) -> None:
        loose = pathlib.Path(self.tmp_root) / "loose"
        loose.mkdir()
        exit_code, out = self.drive(["review", "--target", str(loose), "--task", "Review"])
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "invalid-target")


class CodeExternalTargetTests(WorktreeModeHarness):
    """code confines its worktree + guards to the repo that CONTAINS the --target."""

    def test_code_worktree_and_repository_bind_to_the_target_repo(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self.drive(
            ["code", "--target", str(repo / "pkg"), "--base", "HEAD", "--task", "Fix"],
            repo_root=repo,
            plant=_plant_sentinel,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(pathlib.Path(env["repository"]).resolve(), repo.resolve())
        # The worktree the run created lives under THIS repo's slug in the state
        # root, and its confinement (sentinel, diff, escape scan) is relative to it.
        self.assertEqual(env["targetWorkspace"], "pkg")
        self.assertTrue(pathlib.Path(env["worktreePath"]).is_dir())


if __name__ == "__main__":
    unittest.main()
