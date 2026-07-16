# wrapper/scripts/tests/test_review_isolation.py
#
# Unit + mode tests for opt-in review isolation (design §10 / plan Task 2.4).

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import List
from unittest import mock

from groklib import GrokWrapperError, runstate
from groklib import review_isolation
from groklib import worktree as worktree_mod

from tests import gitfixtures
from tests.modefixtures import ModeHarness, make_review_repo


def _git(repo: pathlib.Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "git {} failed: exit {} stderr={!r}".format(
                args, completed.returncode, completed.stderr
            )
        )
    return completed.stdout


class ReviewIsolationHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-review-iso-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        repo_parent = tempfile.mkdtemp(prefix="repo-", dir=self.tmp_root)
        self.repo = gitfixtures.make_repo(repo_parent)

    def _session(self, run_id: str = None) -> review_isolation.ReviewIsolation:
        rid = run_id or runstate.new_run_id()
        return review_isolation.prepare_review_isolation(repo_root=self.repo, run_id=rid)

    def test_prepare_applies_tracked_dirty_excludes_untracked(self) -> None:
        (self.repo / "a.txt").write_text("alpha\nbeta\ntracked-edit\n", encoding="utf-8")
        (self.repo / "pkg" / "mod.txt").write_text("module\nstaged\n", encoding="utf-8")
        _git(self.repo, "add", "pkg/mod.txt")
        (self.repo / "untracked-only.txt").write_text("never in isolation\n", encoding="utf-8")

        session = self._session()
        try:
            self.assertTrue(session.worktree_path.is_dir())
            self.assertTrue(session.marker_path.is_file())
            marker = session.marker_path.read_text(encoding="utf-8")
            self.assertIn(session.run_id, marker)

            wt_a = (session.worktree_path / "a.txt").read_text(encoding="utf-8")
            self.assertIn("tracked-edit", wt_a)
            wt_mod = (session.worktree_path / "pkg" / "mod.txt").read_text(encoding="utf-8")
            self.assertIn("staged", wt_mod)

            self.assertFalse((session.worktree_path / "dirty.txt").exists())
            self.assertFalse((session.worktree_path / "untracked-only.txt").exists())
        finally:
            review_isolation.cleanup_review_isolation(session)

        self.assertFalse(session.worktree_path.exists())
        self.assertFalse(session.marker_path.exists())
        self.assertFalse(session.diff_path.exists())
        self.assertFalse(worktree_mod._branch_exists(self.repo, session.branch))

    def test_intent_to_add_not_in_isolation(self) -> None:
        new_file = self.repo / "ita-new.txt"
        new_file.write_text("intent to add only\n", encoding="utf-8")
        _git(self.repo, "add", "-N", "ita-new.txt")

        session = self._session()
        try:
            self.assertFalse(
                (session.worktree_path / "ita-new.txt").exists(),
                "git add -N content must not appear in isolation worktree",
            )
        finally:
            review_isolation.cleanup_review_isolation(session)

    def test_worktree_add_failure_is_isolation_unavailable(self) -> None:
        with mock.patch.object(
            worktree_mod,
            "_git",
            side_effect=GrokWrapperError("worktree-failure", "simulated worktree add failure"),
        ):
            with self.assertRaises(GrokWrapperError) as ctx:
                self._session()
        self.assertEqual(ctx.exception.error_class, "isolation-unavailable")

    def test_path_collision_never_reuses(self) -> None:
        run_id = runstate.new_run_id()
        path = runstate.state_root() / "worktrees" / "review" / run_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        with self.assertRaises(GrokWrapperError) as ctx:
            review_isolation.prepare_review_isolation(repo_root=self.repo, run_id=run_id)
        self.assertEqual(ctx.exception.error_class, "isolation-unavailable")
        self.assertIn("already exists", str(ctx.exception).lower())
        shutil.rmtree(path, ignore_errors=True)

    def test_apply_failure_is_isolation_unavailable_and_cleans(self) -> None:
        (self.repo / "a.txt").write_text("alpha\nbeta\nedit\n", encoding="utf-8")
        run_id = runstate.new_run_id()
        real_run = worktree_mod._run_git

        def _flaky(repo, args, **kwargs):
            if args and args[0] == "apply":
                return subprocess.CompletedProcess(
                    args=["git"] + list(args),
                    returncode=1,
                    stdout="",
                    stderr="simulated apply failure",
                )
            return real_run(repo, args, **kwargs)

        with mock.patch.object(worktree_mod, "_run_git", side_effect=_flaky):
            with self.assertRaises(GrokWrapperError) as ctx:
                review_isolation.prepare_review_isolation(repo_root=self.repo, run_id=run_id)
        self.assertEqual(ctx.exception.error_class, "isolation-unavailable")
        wt = runstate.state_root() / "worktrees" / "review" / run_id
        self.assertFalse(wt.exists(), "partial isolation must be cleaned after apply failure")
        self.assertFalse(worktree_mod._branch_exists(self.repo, "grok/review/" + run_id))

    def test_concurrent_isolated_runs(self) -> None:
        (self.repo / "a.txt").write_text("alpha\nbeta\nconcurrent\n", encoding="utf-8")
        results: List[review_isolation.ReviewIsolation] = []
        errors: List[BaseException] = []

        def _one(_i: int) -> None:
            try:
                results.append(self._session())
            except BaseException as exc:  # noqa: BLE001 — collect for assertion
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(_one, range(3)))

        self.assertEqual(errors, [], errors)
        self.assertEqual(len(results), 3)
        paths = {str(s.worktree_path) for s in results}
        self.assertEqual(len(paths), 3)
        for session in results:
            review_isolation.cleanup_review_isolation(session)

    def test_partial_cleanup_is_best_effort(self) -> None:
        session = self._session()
        if session.worktree_path.exists():
            shutil.rmtree(session.worktree_path, ignore_errors=True)
        review_isolation.cleanup_review_isolation(session)
        self.assertFalse(session.marker_path.exists())
        self.assertFalse(session.diff_path.exists())

    def test_non_utf8_tracked_dirty_still_isolates(self) -> None:
        """Tracked dirty with non-UTF-8 bytes must not raise UnicodeDecodeError."""
        # latin-1 / binary-ish content that is not valid UTF-8 as a whole stream
        dirty = b"alpha\n" + bytes([0xC3, 0x28]) + b"\n"  # invalid UTF-8 sequence
        (self.repo / "a.txt").write_bytes(dirty)
        session = self._session()
        try:
            self.assertEqual((session.worktree_path / "a.txt").read_bytes(), dirty)
        finally:
            review_isolation.cleanup_review_isolation(session)

    def test_dirty_submodule_rejected(self) -> None:
        real_run = worktree_mod._run_git
        gitlink_line = (
            ":160000 160000 "
            + ("a" * 40)
            + " "
            + ("b" * 40)
            + " M\tvendor/lib\n"
        )
        fake_raw = subprocess.CompletedProcess(
            args=["git", "diff", "--raw", "HEAD"],
            returncode=0,
            stdout=gitlink_line,
            stderr="",
        )

        def _side_effect(repo, args, **kwargs):
            if args and args[0] == "diff" and "--raw" in args:
                return fake_raw
            return real_run(repo, args, **kwargs)

        with mock.patch.object(worktree_mod, "_run_git", side_effect=_side_effect):
            with self.assertRaises(GrokWrapperError) as ctx:
                self._session()
        self.assertEqual(ctx.exception.error_class, "isolation-unavailable")
        self.assertIn("submodule", str(ctx.exception).lower())


class ReviewIsolationModeTests(ModeHarness):
    """End-to-end mode wiring: live path default, isolated path, failure class."""

    def test_without_isolated_uses_live_cwd_even_with_base(self) -> None:
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--base", "HEAD~0", "--task", "Review"],
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        cwd = self.flag_value(argv, "--cwd")
        self.assertEqual(pathlib.Path(cwd).resolve(), (repo / "pkg").resolve())
        # --base is framing only: prompt mentions base; no isolation worktree left
        prompt = self.read_prompt(argv)
        self.assertIn("Comparison base ref", prompt)
        self.assertIn("HEAD~0", prompt)
        review_wt = pathlib.Path(self.state_home) / "grok-skills" / "worktrees" / "review"
        if review_wt.exists():
            self.assertEqual(list(review_wt.iterdir()), [])

    def test_with_isolated_cwd_is_under_isolation_worktree(self) -> None:
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        (repo / "pkg" / "module.txt").write_text("source under review\nedited\n", encoding="utf-8")
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--isolated", "--task", "Review"],
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        env = json.loads(out)
        self.assertEqual(env["status"], "success")
        argv = self.read_run_argv()
        cwd = pathlib.Path(self.flag_value(argv, "--cwd")).resolve()
        # Isolation cwd is under state_root/worktrees/review/<run_id>/...
        # (cleaned in finally after Grok runs, so assert via the captured argv path
        # shape and that the post-run worktree is gone).
        self.assertEqual(cwd.name, "pkg")
        self.assertIn("/worktrees/review/", str(cwd).replace("\\", "/"))
        run_id = env["runId"]
        state_review = (
            pathlib.Path(self.state_home) / "grok-skills" / "worktrees" / "review"
        ).resolve()
        self.assertFalse((state_review / run_id).exists())
        # Isolation identity was recorded on the run for crash/cleanup recovery
        # (worktree path may already be cleaned; fields must still name the branch).
        record = json.loads(
            (
                pathlib.Path(self.state_home)
                / "grok-skills"
                / "runs"
                / run_id
                / "run.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(record.get("worktreeBranch"), "grok/review/" + run_id)
        self.assertIsNotNone(record.get("worktreePath"))
        self.assertIn("/worktrees/review/" + run_id, str(record.get("worktreePath")))

    def test_isolated_setup_failure_terminalizes_real_run(self) -> None:
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        with mock.patch.object(
            review_isolation,
            "prepare_review_isolation",
            side_effect=GrokWrapperError(
                "isolation-unavailable", "simulated isolation setup failure"
            ),
        ):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--isolated", "--task", "Review"],
                repo_root=repo,
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "isolation-unavailable")
        run_id = env["runId"]
        run_dir = pathlib.Path(self.state_home) / "grok-skills" / "runs" / run_id
        self.assertTrue((run_dir / "envelope.json").is_file())
        record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["runId"], run_id)

    def test_isolated_original_checkout_noise_not_unexpected_edits(self) -> None:
        """Concurrent live-checkout writes must not hard-fail an isolated review."""
        from groklib.modes import _shared

        repo = make_review_repo(pathlib.Path(self.tmp_root))
        real_capture = _shared._capture_review_fs_baseline
        noise_path = repo / "pkg" / "noise-during-review.txt"

        def _capture_then_noise(run, warnings=None):
            baseline = real_capture(run, warnings)
            noise_path.write_text("concurrent editor noise\n", encoding="utf-8")
            return baseline

        with mock.patch.object(_shared, "_capture_review_fs_baseline", _capture_then_noise):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--isolated", "--task", "Review"],
                repo_root=repo,
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")
        err = env.get("error") or {}
        self.assertNotEqual(err.get("class"), "unexpected-edits")
        # Informational drift warnings must not surface live-checkout noise paths.
        for warning in env.get("warnings") or []:
            self.assertNotIn("noise-during-review.txt", str(warning))


if __name__ == "__main__":
    unittest.main()
