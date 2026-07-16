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

    def test_ita_pathspec_metachar_does_not_exclude_tracked_dirty(self) -> None:
        """ITA filename with * must use literal pathspec (not glob-exclude tracked files)."""
        (self.repo / "a.txt").write_text("alpha\nbeta\ntracked-edit\n", encoding="utf-8")
        wild = self.repo / "*.txt"
        wild.write_text("intent only\n", encoding="utf-8")
        _git(self.repo, "add", "-N", "--", "*.txt")

        session = self._session()
        try:
            self.assertFalse((session.worktree_path / "*.txt").exists())
            wt_a = (session.worktree_path / "a.txt").read_text(encoding="utf-8")
            self.assertIn(
                "tracked-edit",
                wt_a,
                "literal ITA exclude must not drop tracked dirty a.txt via glob",
            )
        finally:
            review_isolation.cleanup_review_isolation(session)

    def test_remove_external_worktree_deletes_sibling_diff(self) -> None:
        """cleanup path must reap crash-left {worktree}.diff patch files."""
        from groklib.worktree import ExternalWorktree, remove_external_worktree

        (self.repo / "a.txt").write_text("alpha\nbeta\npatch-me\n", encoding="utf-8")
        session = self._session()
        self.assertTrue(session.diff_path.is_file())
        # Simulate crash: leave worktree+diff, do not call cleanup_review_isolation
        wt = ExternalWorktree(
            path=session.worktree_path,
            branch=session.branch,
            base_revision=session.base_revision,
            repo_root=session.repo_root,
        )
        report = remove_external_worktree(wt, confirmed=True, expected_run_id=session.run_id)
        self.assertTrue(report["removed"])
        self.assertFalse(session.worktree_path.exists())
        self.assertFalse(session.diff_path.exists(), "sibling .diff must be removed by cleanup")
        self.assertFalse(session.marker_path.exists())

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
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
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
            args=["git", "diff", "--raw", "--ignore-submodules=none", "HEAD"],
            returncode=0,
            stdout=gitlink_line,
            stderr="",
        )
        seen_ignore = []

        def _side_effect(repo, args, **kwargs):
            if args and args[0] == "diff" and "--raw" in args:
                seen_ignore.append("--ignore-submodules=none" in args)
                return fake_raw
            return real_run(repo, args, **kwargs)

        with mock.patch.object(worktree_mod, "_run_git", side_effect=_side_effect):
            with self.assertRaises(GrokWrapperError) as ctx:
                self._session()
        self.assertEqual(ctx.exception.error_class, "isolation-unavailable")
        self.assertIn("submodule", str(ctx.exception).lower())
        self.assertTrue(seen_ignore and all(seen_ignore), "must force ignore-submodules=none")

    def test_isolation_diff_disables_external_diff(self) -> None:
        """Snapshot capture must use internal git diff (no GIT_EXTERNAL_DIFF / textconv)."""
        (self.repo / "a.txt").write_text("alpha\nbeta\nedit\n", encoding="utf-8")
        captured = []
        real = review_isolation._run_git_bytes

        def _wrap(repo, args, **kwargs):
            captured.append(list(args))
            return real(repo, args, **kwargs)

        with mock.patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": "/bin/false"}):
            with mock.patch.object(review_isolation, "_run_git_bytes", side_effect=_wrap):
                session = self._session()
                review_isolation.cleanup_review_isolation(session)
        self.assertTrue(captured)
        for args in captured:
            if args and args[0] == "diff":
                self.assertIn("--no-ext-diff", args)
                self.assertIn("--no-textconv", args)
                break
        else:
            self.fail("expected a git diff invocation for isolation patch")

    def test_dirty_patch_uses_pinned_base_not_live_head(self) -> None:
        """Diff must target the worktree base_sha, not symbolic HEAD (concurrent moves)."""
        (self.repo / "a.txt").write_text("alpha\nbeta\nedit\n", encoding="utf-8")
        head_before = _git(self.repo, "rev-parse", "HEAD").strip()
        captured_diff_bases = []
        real = review_isolation._run_git_bytes

        def _wrap(repo, args, **kwargs):
            if args and args[0] == "diff" and "--binary" in args:
                # argv: diff ... <base> -- .
                try:
                    dash = args.index("--")
                    captured_diff_bases.append(args[dash - 1])
                except ValueError:
                    pass
            return real(repo, args, **kwargs)

        with mock.patch.object(review_isolation, "_run_git_bytes", side_effect=_wrap):
            session = self._session()
            try:
                self.assertEqual(session.base_revision, head_before)
                self.assertIn(head_before, captured_diff_bases)
                self.assertNotIn("HEAD", captured_diff_bases)
            finally:
                review_isolation.cleanup_review_isolation(session)

    def test_owner_marker_written_before_worktree_add(self) -> None:
        """Crash window: marker must exist before git worktree add returns."""
        order = []
        real_write = runstate.write_owner_marker_file
        real_git = worktree_mod._git

        def _write(path, run_id):
            order.append("marker")
            return real_write(path, run_id)

        def _git(repo, *args, **kwargs):
            if args and args[0] == "worktree" and args[1] == "add":
                order.append("worktree-add")
                # Marker must already exist for the planned path.
                # Find path arg: worktree add -b branch path base
                wt_path = pathlib.Path(args[4])
                marker = worktree_mod.marker_path_for(wt_path)
                self.assertTrue(marker.is_file(), "owner marker must precede worktree add")
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(runstate, "write_owner_marker_file", _write):
            with mock.patch.object(worktree_mod, "_git", _git):
                session = self._session()
                review_isolation.cleanup_review_isolation(session)
        self.assertEqual(order[:2], ["marker", "worktree-add"])

    def test_cleanup_retains_marker_if_worktree_still_present(self) -> None:
        session = self._session()
        # Force path to look non-removable: leave a marker while path still exists
        # by mocking rmtree/remove to no-op.
        with mock.patch.object(
            worktree_mod,
            "_run_git",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="locked"),
        ):
            with mock.patch("shutil.rmtree", lambda *a, **k: None):
                # path still exists after failed remove
                review_isolation.cleanup_review_isolation(session)
        self.assertTrue(session.worktree_path.exists())
        self.assertTrue(
            session.marker_path.is_file(),
            "marker must remain while worktree exists for cleanup --confirm",
        )
        # Force real cleanup for teardown
        import shutil

        shutil.rmtree(session.worktree_path, ignore_errors=True)
        if session.marker_path.exists():
            session.marker_path.unlink()
        if session.diff_path.exists():
            session.diff_path.unlink()
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "-D", session.branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def test_is_all_zero_oid_accepts_sha256_length(self) -> None:
        self.assertTrue(review_isolation._is_all_zero_oid("0" * 40))
        self.assertTrue(review_isolation._is_all_zero_oid("0" * 64))
        self.assertFalse(review_isolation._is_all_zero_oid("0" * 39))
        self.assertFalse(review_isolation._is_all_zero_oid("a" * 40))


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

    def test_isolation_identity_recorded_before_prepare(self) -> None:
        """run.json must name the worktree before prepare creates it (crash window)."""
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        recorded = {}
        real_prepare = review_isolation.prepare_review_isolation

        def _prepare(**kwargs):
            run_id = kwargs["run_id"]
            run_dir = pathlib.Path(self.state_home) / "grok-skills" / "runs" / run_id
            record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            recorded["worktreePath"] = record.get("worktreePath")
            recorded["worktreeBranch"] = record.get("worktreeBranch")
            recorded["baseRevision"] = record.get("baseRevision")
            # Path must already be planned and not yet exist (prepare creates it).
            self.assertIsNotNone(recorded["worktreePath"])
            self.assertFalse(pathlib.Path(recorded["worktreePath"]).exists())
            return real_prepare(**kwargs)

        with mock.patch.object(review_isolation, "prepare_review_isolation", _prepare):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--isolated", "--task", "Review"],
                repo_root=repo,
            )
        self.assertEqual(exit_code, 0, out)
        self.assertTrue(str(recorded["worktreePath"]).endswith("/worktrees/review/" + json.loads(out)["runId"]))
        self.assertEqual(recorded["worktreeBranch"], "grok/review/" + json.loads(out)["runId"])
        self.assertTrue(isinstance(recorded["baseRevision"], str) and len(recorded["baseRevision"]) >= 7)

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

    def test_isolated_rules_come_from_snapshot_not_live_untracked(self) -> None:
        """Untracked AGENTS.md on the live tree must not govern --isolated prompts."""
        repo = make_review_repo(pathlib.Path(self.tmp_root))
        # Drop AGENTS.md from HEAD so isolation snapshot has only CLAUDE.md.
        # Leave an untracked AGENTS.md on the live tree with a unique marker.
        subprocess.run(
            ["git", "-C", str(repo), "rm", "-f", "AGENTS.md"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "drop AGENTS for isolation rules test"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (repo / "AGENTS.md").write_text(
            "<!-- AGENTS.md | CLAUDE.md -->\n=== LIVE UNTRACKED RULES MARKER ===\n",
            encoding="utf-8",
        )
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--isolated", "--task", "Review"],
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        prompt = self.read_prompt(self.read_run_argv())
        self.assertNotIn(
            "LIVE UNTRACKED RULES MARKER",
            prompt,
            "isolated review must not load untracked live AGENTS.md into the prompt",
        )
        self.assertIn("Always read the rules before acting.", prompt)

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
