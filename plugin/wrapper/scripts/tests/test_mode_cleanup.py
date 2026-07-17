# wrapper/scripts/tests/test_mode_cleanup.py

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import grok_agent
from groklib import GrokWrapperError, runstate
from groklib.modes import cleanup as cleanup_mod
from groklib.worktree import create_external_worktree

from tests import gitfixtures


def _run_cleanup(run_id, confirm=False):
    argv = ["cleanup", "--run-id", run_id]
    if confirm:
        argv.append("--confirm")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = grok_agent.main(argv)
    return exit_code, buffer.getvalue()


class RebuildWorktreeDelegationTests(unittest.TestCase):
    """cleanup rebuilds via worktree.rebuild_worktree_from_record (single source)."""

    def test_cleanup_rebuild_delegates_to_worktree_helper(self) -> None:
        record = {
            "worktreePath": "/tmp/wt",
            "worktreeBranch": "grok/code/x",
            "baseRevision": "a" * 40,
            "repository": "/tmp/repo",
        }
        # cleanup binds rebuild_worktree_from_record at import; patch that name.
        with mock.patch.object(
            cleanup_mod,
            "rebuild_worktree_from_record",
            wraps=cleanup_mod.rebuild_worktree_from_record,
        ) as rebuilt:
            result = cleanup_mod._rebuild_worktree(record)
            rebuilt.assert_called_once_with(record)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(str(result.path), "/tmp/wt")
            self.assertEqual(result.branch, "grok/code/x")


class CleanupModeTests(unittest.TestCase):
    """cleanup verifies ownership, reports dry-run artifacts, and removes on --confirm."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-cleanup-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        repo_parent = tempfile.mkdtemp(prefix="grok-cli-repo-", dir=self.tmp_root)
        self.repo_root = gitfixtures.make_repo(repo_parent)
        self.base = gitfixtures.head_revision(self.repo_root)

    def _write_record(self, paths, worktree=None):
        record = {
            "schemaVersion": 1,
            "runId": paths.run_id,
            "mode": "code",
            "createdAtUtc": "2026-07-14T00:00:00+00:00",
            "status": "success",
            "requestedModel": "grok-4.5",
            "repository": str(self.repo_root) if worktree else None,
            "targetWorkspace": None,
            "worktreePath": str(worktree.path) if worktree else None,
            "worktreeBranch": worktree.branch if worktree else None,
            "baseRevision": worktree.base_revision if worktree else None,
            "progressStreamPath": str(paths.progress_path),
            "envelopePath": str(paths.envelope_path),
        }
        # CAS seed running metadata
        rec = runstate.load_run_record(paths.run_id)
        rev = int(rec.get("recordRevision", 0))
        if rec.get("lifecycle") == "created":
            rec = runstate.set_lifecycle(paths, rev, "running")
            rev = int(rec["recordRevision"])
        runstate.cas_update_run_record(paths, rev, {"status": "running", "requestedModel": record.get("requestedModel"), "worktreePath": record.get("worktreePath"), "worktreeBranch": record.get("worktreeBranch"), "baseRevision": record.get("baseRevision"), "repository": record.get("repository")})


    def _terminalize(self, paths, status="success"):
        """Mark run terminal so cleanup --confirm is allowed with a live test pid."""
        from groklib.envelope import build_envelope, failure_envelope
        rec = runstate.load_run_record(paths.run_id)
        rev = int(rec.get("recordRevision", 0))
        life = rec.get("lifecycle")
        if life == "created":
            rec = runstate.set_lifecycle(paths, rev, "running")
            rev = int(rec["recordRevision"])
            life = "running"
        if life == "running":
            rec = runstate.set_lifecycle(paths, rev, "finalizing")
            rev = int(rec["recordRevision"])
        if status == "success":
            env = build_envelope(run_id=paths.run_id, mode=rec.get("mode") or "code", status="success", response={"ok": True})
            runstate.persist_terminal_envelope(paths, rev, env, lifecycle="completed")
        else:
            env = failure_envelope(run_id=paths.run_id, mode=rec.get("mode") or "code", error_class="cli-failure", message="x")
            runstate.persist_terminal_envelope(paths, rev, env, lifecycle="failed")

    def _seed_run_without_worktree(self, terminal=True):
        paths = runstate.create_run("code")
        self._write_record(paths, worktree=None)
        if terminal:
            self._terminalize(paths)
        return paths

    def _seed_run_with_worktree(self, terminal=True):
        paths = runstate.create_run("code")
        worktree = create_external_worktree(repo_root=self.repo_root, base=self.base, run_id=paths.run_id)
        self.addCleanup(self._force_remove_worktree, worktree)
        self._write_record(paths, worktree=worktree)
        if terminal:
            self._terminalize(paths)
        return paths, worktree

    def _force_remove_worktree(self, worktree):
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(worktree.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_root), "branch", "-D", worktree.branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        marker = pathlib.Path(str(worktree.path) + ".owner.json")
        if marker.exists():
            marker.unlink()

    def test_cleanup_dry_run_lists_owned_artifacts_without_removal(self) -> None:
        paths, worktree = self._seed_run_with_worktree()
        exit_code, out = _run_cleanup(paths.run_id, confirm=False)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["cleanup"]["status"], "retained")
        self.assertEqual(envelope["worktreePath"], str(worktree.path))
        self.assertTrue(paths.run_dir.exists())
        self.assertTrue(worktree.path.exists())

    def test_cleanup_confirm_removes_run_dir(self) -> None:
        paths = self._seed_run_without_worktree()
        self.assertTrue(paths.run_dir.exists())
        exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["cleanup"]["status"], "clean")
        self.assertFalse(paths.run_dir.exists())

    def test_cleanup_confirm_removes_run_dir_when_worktree_path_already_gone(self) -> None:
        # Grok dogfood-4 #1 cleanup-wedge: the run recorded a worktree, but the
        # worktree DIRECTORY is already gone (operator rm, or a crash after
        # `git worktree remove` before the run-dir delete). cleanup --confirm must
        # treat the missing worktree as already-removed and STILL delete runs/<id>/
        # (which may still hold progress.jsonl), not wedge on worktree-failure.
        paths, worktree = self._seed_run_with_worktree()
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(worktree.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertFalse(worktree.path.exists())
        self.assertTrue(paths.run_dir.exists())

        exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertFalse(paths.run_dir.exists(), "the run dir must be reaped once the worktree is gone")

    def _make_partial_create_dir(self) -> tuple:
        """A create_run that died before owner.json/run.json existed: a bare 0700 run dir."""
        run_id = runstate.new_run_id()
        run_dir = runstate.state_root() / "runs" / run_id
        run_dir.mkdir(parents=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        return run_id, run_dir

    def test_cleanup_refuses_when_record_points_at_another_runs_worktree(self) -> None:
        # PR968 codex #4: a stale/corrupt run.json for run A points at run B's
        # worktree. cleanup --run-id A --confirm must refuse (B's marker names B,
        # not the requested A) rather than destroy B's worktree/branch/marker.
        paths_b, worktree_b = self._seed_run_with_worktree()
        paths_a = runstate.create_run("code")
        self._write_record(paths_a, worktree=worktree_b)  # A's record -> B's worktree
        # Terminalize A so cleanup proceeds past the active-run guard into the
        # worktree ownership check (the subject of this test).
        self._terminalize(paths_a)

        exit_code, out = _run_cleanup(paths_a.run_id, confirm=True)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "state-ownership-violation")
        self.assertEqual(envelope["error"]["detail"]["requestedRunId"], paths_a.run_id)
        self.assertEqual(envelope["error"]["detail"]["worktreeRunId"], worktree_b.path.name)
        # Run B's worktree + marker and run A's dir all survive untouched.
        self.assertTrue(worktree_b.path.exists())
        self.assertTrue(pathlib.Path(str(worktree_b.path) + ".owner.json").exists())
        self.assertTrue(paths_a.run_dir.exists())

    def test_cleanup_reaps_partial_create_dir_with_confirm(self) -> None:
        # Round5 unreapable-run-dir-on-create-run-partial-failure: a run dir whose
        # create_run failed before a valid owner.json/run.json is reapable by the
        # wrapper's own cleanup, not permanent debris only rm -rf can clear.
        run_id, run_dir = self._make_partial_create_dir()
        self.assertTrue(run_dir.exists())
        exit_code, out = _run_cleanup(run_id, confirm=True)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["cleanup"]["status"], "clean")
        self.assertTrue(envelope["response"]["partialCreate"])
        self.assertFalse(run_dir.exists())

    def test_cleanup_partial_create_dir_dry_run_retains(self) -> None:
        run_id, run_dir = self._make_partial_create_dir()
        exit_code, out = _run_cleanup(run_id, confirm=False)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["cleanup"]["status"], "retained")
        self.assertTrue(envelope["response"]["partialCreate"])
        self.assertTrue(run_dir.exists(), "dry-run must not remove the partial dir")

    def test_cleanup_does_not_treat_valid_marked_run_as_partial_debris(self) -> None:
        # A dir with a VALID owner marker but no run.json AND no liveness lease
        # (unknown owner -> possibly-active) must NOT be reaped as partial debris;
        # it is protected via the invalid-target path, never removed.
        run_id, run_dir = self._make_partial_create_dir()
        runstate.write_owner_marker(run_dir, run_id)  # a real, valid marker; no lease
        exit_code, out = _run_cleanup(run_id, confirm=True)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "invalid-target")
        self.assertTrue(run_dir.exists(), "an in-flight run's dir must not be removed")

    def test_cleanup_reaps_post_create_crash_dir_with_dead_owner(self) -> None:
        # F4-partial-create: create_run wrote the owner marker + a liveness lease,
        # then the caller crashed before run.json. A DEAD owner lease makes the
        # orphaned dir reapable by cleanup --confirm (it was permanently stuck
        # before, since a valid marker was treated as an in-flight run).
        import subprocess
        import sys

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        dead_pid = child.pid

        run_id, run_dir = self._make_partial_create_dir()
        runstate.write_owner_marker(run_dir, run_id)
        runstate.write_home_liveness_marker(run_dir, dead_pid)
        exit_code, out = _run_cleanup(run_id, confirm=True)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertTrue(envelope["response"]["partialCreate"])
        self.assertFalse(run_dir.exists(), "a dead-owner post-create crash dir must be reaped")

    def test_cleanup_protects_live_post_create_dir_with_alive_owner(self) -> None:
        # The converse: a valid-marker + no-run.json dir whose owner lease is ALIVE
        # (an in-flight create) must NEVER be reaped.
        run_id, run_dir = self._make_partial_create_dir()
        runstate.write_owner_marker(run_dir, run_id)
        runstate.write_home_liveness_marker(run_dir, os.getpid())
        exit_code, out = _run_cleanup(run_id, confirm=True)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "invalid-target")
        self.assertTrue(run_dir.exists(), "a live in-flight create must not be removed")

    def test_cleanup_wrong_owner_fails_closed(self) -> None:
        paths = self._seed_run_without_worktree()
        # Tamper the run-dir ownership marker so it no longer matches the wrapper.
        (paths.run_dir / "owner.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "owner": "someone-else",
                    "runId": paths.run_id,
                    "createdAtUtc": "2026-07-14T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["error"]["class"], "state-ownership-violation")
        self.assertTrue(paths.run_dir.exists())

    def test_cleanup_confirm_reports_retained_when_branch_retained(self) -> None:
        # S5: when the worktree is removed but its branch has unmerged Grok
        # commits and is retained, the top-level cleanup.status must say
        # "retained", never falsely claim "clean".
        paths, worktree = self._seed_run_with_worktree()
        # Commit inside the worktree so its branch has a commit not on HEAD; the
        # worktree stays clean (committed) so the dirty-refusal guard is inert.
        (worktree.path / "unmerged.txt").write_text("grok wrote this\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree.path), "add", "-A"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(worktree.path), "commit", "-q", "-m", "commit inside worktree"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["cleanup"]["status"], "retained")
        self.assertIn("retained", envelope["cleanup"]["detail"])
        self.assertFalse(paths.run_dir.exists())
        self.assertFalse(worktree.path.exists())

    def test_cleanup_marker_runid_mismatch_fails_closed(self) -> None:
        # S6: the run-dir marker must NAME the requested run id. A marker with the
        # correct owner/shape but a different runId must fail closed.
        paths = self._seed_run_without_worktree()
        other_run_id = runstate.new_run_id()
        runstate.write_owner_marker_file(paths.run_dir / "owner.json", other_run_id)
        exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "state-ownership-violation")
        self.assertTrue(paths.run_dir.exists())

    def test_cleanup_confirm_partial_when_run_dir_removal_fails(self) -> None:
        # F5: if run-dir removal fails AFTER the worktree was removed, the partial
        # state is reported honestly ("failed" + worktreeRemoved=true), never left
        # as a misleading "not-applicable" cleanup.
        paths, worktree = self._seed_run_with_worktree()

        def _boom(run_dir):
            raise GrokWrapperError(
                "cleanup-failure", "simulated run dir removal failure", {"runDir": str(run_dir)}
            )

        with mock.patch.object(cleanup_mod, "_remove_run_dir", _boom):
            exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "cleanup-failure")
        self.assertEqual(envelope["cleanup"]["status"], "failed")
        self.assertNotEqual(envelope["cleanup"]["status"], "not-applicable")
        self.assertTrue(envelope["response"]["worktreeRemoved"])
        self.assertFalse(envelope["response"]["runDirRemoved"])
        # The worktree really was removed before the run-dir removal failed.
        self.assertFalse(worktree.path.exists())

    def test_cleanup_confirm_retryable_after_run_dir_removal_fails_once(self) -> None:
        # PR968 codex cleanup-retryable: when --confirm removes the worktree + sibling
        # marker but the run-dir delete then FAILS, the retained run.json points at an
        # already-gone worktree. A retry must NOT wedge on the now-absent sibling marker
        # (which would return state-ownership-violation forever); the second --confirm
        # must complete and remove the run dir.
        paths, worktree = self._seed_run_with_worktree()

        calls = {"n": 0}
        real_remove = cleanup_mod._remove_run_dir

        def _fail_once(run_dir):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GrokWrapperError(
                    "cleanup-failure", "simulated run dir removal failure", {"runDir": str(run_dir)}
                )
            return real_remove(run_dir)

        with mock.patch.object(cleanup_mod, "_remove_run_dir", _fail_once):
            # First attempt: worktree + sibling marker removed, run-dir delete fails.
            exit_code, out = _run_cleanup(paths.run_id, confirm=True)
            first = json.loads(out)
            self.assertEqual(exit_code, 1, out)
            self.assertEqual(first["status"], "failure")
            self.assertTrue(first["response"]["worktreeRemoved"])
            self.assertFalse(first["response"]["runDirRemoved"])
            self.assertFalse(worktree.path.exists(), "the worktree was removed before the run-dir failure")
            self.assertTrue(paths.run_dir.exists(), "the run dir survives the first failed delete")

            # Retry: the sibling marker is gone, but cleanup must complete, not wedge.
            exit_code, out = _run_cleanup(paths.run_id, confirm=True)
            second = json.loads(out)

        self.assertEqual(exit_code, 0, out)
        self.assertEqual(second["status"], "success")
        self.assertTrue(second["response"]["runDirRemoved"])
        self.assertFalse(paths.run_dir.exists(), "the retry must reap the run dir")

    def test_confirm_refuses_nonterminal_when_owner_alive(self) -> None:
        paths = self._seed_run_without_worktree(terminal=False)
        # create_run already wrote a live owner.pid; lifecycle is non-terminal.
        runstate.write_home_liveness_marker(paths.run_dir, os.getpid())
        code, out = _run_cleanup(paths.run_id, confirm=True)
        self.assertEqual(code, 1)
        env = json.loads(out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "state-ownership-violation")
        self.assertTrue(paths.run_dir.is_dir())

    def test_cleanup_confirm_removes_dirty_owner_marked_worktree(self) -> None:
        # Grok dogfood-2 #8: code mode leaves the worktree dirty by design, so a
        # confirmed cleanup of an OWNER-MARKED run must fully complete -- remove
        # the dirty worktree AND the run dir -- not refuse and retain everything.
        paths, worktree = self._seed_run_with_worktree()
        (worktree.path / "grok_uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        exit_code, out = _run_cleanup(paths.run_id, confirm=True)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertTrue(envelope["response"]["worktreeRemoved"])
        self.assertTrue(envelope["response"]["runDirRemoved"])
        self.assertFalse(worktree.path.exists())
        self.assertFalse(paths.run_dir.exists())


if __name__ == "__main__":
    unittest.main()
