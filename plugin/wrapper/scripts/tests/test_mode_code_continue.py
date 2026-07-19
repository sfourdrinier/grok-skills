# wrapper/scripts/tests/test_mode_code_continue.py
#
# code --continue-run tests, split from test_mode_code.py (900-line cap).
# Pure move; shared harness stays in the sibling module.

import json
import os
import pathlib
import unittest
from unittest import mock
from groklib import envelope as envelope_mod
from groklib import grokcli
from groklib import platformsupport
from groklib import runstate
from groklib import sandbox
from groklib import worktree as worktree_mod
from groklib.modes import _envelope, _shared, _worktree
from tests.worktreefixtures import WorktreeModeHarness
from tests.test_mode_code import _plant_sentinel_in_worktree


class ContinueRunTests(WorktreeModeHarness):
    """code --continue-run: mutual exclusion, prior validation, directive, ref-read."""

    def _run(self, repo, extra_argv=None, **kwargs):
        argv = ["code", "--target", "pkg", "--base", "HEAD", "--task", "Fix the module"]
        if extra_argv:
            argv = argv[:1] + extra_argv + argv[1:]
        return self.drive(argv, repo_root=repo, **kwargs)

    def test_continue_run_rejects_target_and_base(self) -> None:
        repo = self.make_code_repo()
        rid = "20260716T000000Z-abc123"
        exit_code, out = self.drive(
            [
                "code",
                "--continue-run",
                rid,
                "--target",
                "pkg",
                "--base",
                "HEAD",
                "--task",
                "follow up",
            ],
            repo_root=repo,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "usage-error")
        self.assertRegex(env["error"]["message"].lower(), r"continue-run|target|base")

    def test_continue_run_rejects_contract_file(self) -> None:
        repo = self.make_code_repo()
        cpath = pathlib.Path(self.tmp_root) / "c.json"
        cpath.write_text("{}", encoding="utf-8")
        exit_code, out = self.drive(
            [
                "code",
                "--continue-run",
                "20260716T000000Z-abc123",
                "--contract-file",
                str(cpath),
                "--task",
                "follow up",
            ],
            repo_root=repo,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "usage-error")

    def test_code_without_target_base_or_continue_is_usage_error(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self.drive(
            ["code", "--task", "no anchors"],
            repo_root=repo,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "usage-error")

    def test_continue_run_unknown_run_id_fails_invalid_target(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self.drive(
            [
                "code",
                "--continue-run",
                "20260716T000000Z-abc123",
                "--task",
                "follow up",
            ],
            repo_root=repo,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "invalid-target")
        self.assertIn("20260716T000000Z-abc123", env["error"]["message"])

    def test_continue_run_non_terminal_prior_fails_invalid_target(self) -> None:
        repo = self.make_code_repo()
        paths = runstate.create_run("code")
        record = runstate.load_run_record(paths.run_id)
        rev = int(record.get("recordRevision", 0))
        runstate.cas_update_run_record(
            paths,
            rev,
            {
                "mode": "code",
                "repository": str(repo),
                "targetWorkspace": "pkg",
                "status": "running",
            },
        )
        # lifecycle stays non-terminal (created/running)
        exit_code, out = self.drive(
            ["code", "--continue-run", paths.run_id, "--task", "follow up"],
            repo_root=repo,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "invalid-target")
        self.assertIn(paths.run_id, env["error"]["message"])

    def test_continue_run_missing_worktree_fails_invalid_target(self) -> None:
        from groklib.envelope import build_envelope

        repo = self.make_code_repo()
        paths = runstate.create_run("code")
        record = runstate.load_run_record(paths.run_id)
        rev = int(record.get("recordRevision", 0))
        runstate.cas_update_run_record(
            paths,
            rev,
            {
                "mode": "code",
                "repository": str(repo),
                "targetWorkspace": "pkg",
                "worktreePath": str(pathlib.Path(self.tmp_root) / "gone-wt"),
                "worktreeBranch": "grok/code/" + paths.run_id,
                "baseRevision": "a" * 40,
                "status": "running",
            },
        )
        # Force terminal lifecycle so missing-worktree is the failing check.
        record = runstate.load_run_record(paths.run_id)
        rev = int(record["recordRevision"])
        if record.get("lifecycle") == "created":
            record = runstate.set_lifecycle(paths, rev, "running")
            rev = int(record["recordRevision"])
        if record.get("lifecycle") == "running":
            record = runstate.set_lifecycle(paths, rev, "finalizing")
            rev = int(record["recordRevision"])
        env_stub = build_envelope(
            run_id=paths.run_id, mode="code", status="success", response={"ok": True}
        )
        runstate.persist_terminal_envelope(paths, rev, env_stub, lifecycle="completed")
        exit_code, out = self.drive(
            ["code", "--continue-run", paths.run_id, "--task", "follow up"],
            repo_root=repo,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "invalid-target")
        self.assertIn(paths.run_id, env["error"]["message"])
        self.assertRegex(env["error"]["message"].lower(), r"worktree")

    def test_continuation_directive_names_iteration_and_prior_run(self) -> None:
        from groklib.modes import code as code_mode

        text = code_mode._continuation_directive("20260716T000000Z-abc123", 1)
        self.assertIn("iteration 2", text)
        self.assertIn("20260716T000000Z-abc123", text)
        self.assertIn("SAME isolated", text)

    def test_read_committed_manifest_fields_from_ref(self) -> None:
        import subprocess

        from groklib.modes import code as code_mode

        repo = self.make_code_repo()
        # Edit package.json in the checkout (simulates post-Grok worktree edits).
        manifest_path = repo / "pkg" / "package.json"
        edited = {"name": "pkg-RENAMED-by-grok", "scripts": {"build": "echo hijacked"}}
        manifest_path.write_text(json.dumps(edited), encoding="utf-8")
        base = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        name, scripts = code_mode._read_committed_manifest_fields_from_ref(
            repo, base, "pkg"
        )
        self.assertEqual(name, "pkg-under-test")
        self.assertIsInstance(scripts, dict)
        self.assertEqual(scripts.get("build"), "true")
        # Missing path returns (None, None)
        missing_name, missing_scripts = code_mode._read_committed_manifest_fields_from_ref(
            repo, base, "no-such-pkg"
        )
        self.assertIsNone(missing_name)
        self.assertIsNone(missing_scripts)

    def test_contract_json_persisted_on_initial_code_run(self) -> None:
        repo = self.make_code_repo()
        contract = {
            "schemaVersion": 1,
            "taskId": "persist-c",
            "objective": "persist me",
            "target": "pkg",
            "writeScopes": [{"kind": "subtree", "path": "pkg"}],
            "acceptanceCriteria": ["ok"],
            "requiredValidation": [],
        }
        cpath = pathlib.Path(self.tmp_root) / "contract-persist.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("x\n", encoding="utf-8")

        exit_code, out = self._run(
            repo,
            extra_argv=["--contract-file", str(cpath)],
            plant=plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        run_id = env["runId"]
        persisted = runstate.state_root() / "runs" / run_id / "contract.json"
        self.assertTrue(persisted.is_file(), "contract.json must be written next to run artifacts")
        loaded = json.loads(persisted.read_text(encoding="utf-8"))
        self.assertEqual(loaded.get("taskId"), "persist-c")
        self.assertEqual(loaded.get("objective"), "persist me")
        mode = persisted.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_continue_run_reuses_worktree_and_writes_lineage(self) -> None:
        from groklib import session_store

        repo = self.make_code_repo()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v1\n", encoding="utf-8")

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]
        prior_wt = env["worktreePath"]
        self.assertTrue(pathlib.Path(prior_wt).is_dir())

        # Seed a session archive so continuation can resume.
        prior_dir = runstate.state_root() / "runs" / prior_id
        home = pathlib.Path(self.tmp_root) / "seed-home"
        sessions = home / ".grok" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "prompt_history.jsonl").write_text("{}\n", encoding="utf-8")
        session_store.archive_session(home, prior_dir, "11111111-1111-4111-8111-111111111111")

        def plant2(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v2\n", encoding="utf-8")

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "iterate"],
            repo_root=repo,
            plant=plant2,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 0, out2)
        self.assertEqual(env2["worktreePath"], prior_wt)
        new_id = env2["runId"]
        self.assertNotEqual(new_id, prior_id)
        new_record = runstate.load_run_record(new_id)
        self.assertEqual(new_record.get("continuesRunId"), prior_id)
        self.assertEqual(new_record.get("iteration"), 2)
        manifest_path = runstate.state_root() / "runs" / new_id / "implementation-handoff.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("continuesRunId"), prior_id)
        self.assertEqual(manifest.get("iteration"), 2)

    def test_continue_run_missing_session_archive_warns_and_fresh_session(self) -> None:
        repo = self.make_code_repo()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v1\n", encoding="utf-8")

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]

        def plant2(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "iterate without archive"],
            repo_root=repo,
            plant=plant2,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 0, out2)
        warnings = env2.get("warnings") or []
        joined = " ".join(warnings)
        self.assertIn(
            "prior run has no session archive; continuing in the same worktree with a fresh Grok session",
            joined,
        )

    def test_second_continue_of_same_prior_fails_naming_child(self) -> None:
        """Finding 2: single-lineage - second continue of A fails naming B."""
        repo = self.make_code_repo()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v1\n", encoding="utf-8")

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]

        def plant2(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v2\n", encoding="utf-8")

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "first continue"],
            repo_root=repo,
            plant=plant2,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 0, out2)
        child_id = env2["runId"]
        prior_record = runstate.load_run_record(prior_id)
        self.assertEqual(prior_record.get("continuedByRunId"), child_id)

        exit_code3, out3 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "fork sibling"],
            repo_root=repo,
            plant=plant2,
        )
        env3 = json.loads(out3)
        self.assertEqual(exit_code3, 1, out3)
        self.assertEqual(env3["error"]["class"], "invalid-target")
        msg = env3["error"]["message"]
        self.assertIn(prior_id, msg)
        self.assertIn(child_id, msg)
        self.assertRegex(msg.lower(), r"already continued")

    def test_missing_persisted_contract_when_prior_had_one_fails(self) -> None:
        """Finding 3: silent contract loss fails closed."""
        repo = self.make_code_repo()
        contract = {
            "schemaVersion": 1,
            "taskId": "lost-c",
            "objective": "must not lose me",
            "target": "pkg",
            "writeScopes": [{"kind": "subtree", "path": "pkg"}],
            "acceptanceCriteria": ["ok"],
            "requiredValidation": [],
        }
        cpath = pathlib.Path(self.tmp_root) / "contract-lost.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("x\n", encoding="utf-8")

        exit_code, out = self._run(
            repo,
            extra_argv=["--contract-file", str(cpath)],
            plant=plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]
        prior_dir = runstate.state_root() / "runs" / prior_id
        manifest = json.loads(
            (prior_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        self.assertTrue(manifest.get("contractSha256"))
        (prior_dir / "contract.json").unlink()

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "should fail"],
            repo_root=repo,
            plant=plant,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 1, out2)
        self.assertEqual(env2["error"]["class"], "implementation-contract-invalid")
        self.assertRegex(
            env2["error"]["message"].lower(),
            r"prior run had a contract but its persisted copy is missing",
        )

    def test_contract_sha_mismatch_on_continue_fails(self) -> None:
        """Finding 4: tampered/replaced contract.json fails closed on sha pin."""
        repo = self.make_code_repo()
        contract = {
            "schemaVersion": 1,
            "taskId": "pin-c",
            "objective": "pin me",
            "target": "pkg",
            "writeScopes": [{"kind": "subtree", "path": "pkg"}],
            "acceptanceCriteria": ["ok"],
            "requiredValidation": [],
        }
        cpath = pathlib.Path(self.tmp_root) / "contract-pin.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("x\n", encoding="utf-8")

        exit_code, out = self._run(
            repo,
            extra_argv=["--contract-file", str(cpath)],
            plant=plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]
        prior_dir = runstate.state_root() / "runs" / prior_id
        # Replace with a different valid contract (same shape, different taskId).
        tampered = dict(contract)
        tampered["taskId"] = "tampered"
        (prior_dir / "contract.json").write_text(
            json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "should fail sha"],
            repo_root=repo,
            plant=plant,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 1, out2)
        self.assertEqual(env2["error"]["class"], "implementation-contract-invalid")
        self.assertRegex(
            env2["error"]["message"].lower(),
            r"does not match the prior run's contractsha256",
        )

    def test_invalid_prior_base_fails_at_entry(self) -> None:
        """Finding 5: baseRevision that is not a commit fails invalid-target early."""
        repo = self.make_code_repo()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v1\n", encoding="utf-8")

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]
        prior_dir = runstate.state_root() / "runs" / prior_id
        record_path = prior_dir / "run.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        bogus = "deadbeef" * 5
        record["baseRevision"] = bogus
        runstate.write_json_atomic(record_path, record)

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "bad base"],
            repo_root=repo,
            plant=plant,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 1, out2)
        self.assertEqual(env2["error"]["class"], "invalid-target")
        self.assertIn(bogus, env2["error"]["message"])

    def test_iteration_cap_enforced_with_usage_error(self) -> None:
        """Finding 6: continuing past MAX_CONTINUATION_ITERATION is usage-error."""
        from groklib.modes import code_continue

        repo = self.make_code_repo()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "impl.txt").write_text("v1\n", encoding="utf-8")

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        prior_id = env["runId"]
        prior_dir = runstate.state_root() / "runs" / prior_id
        record_path = prior_dir / "run.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["iteration"] = code_continue.MAX_CONTINUATION_ITERATION
        runstate.write_json_atomic(record_path, record)

        exit_code2, out2 = self.drive(
            ["code", "--continue-run", prior_id, "--task", "over cap"],
            repo_root=repo,
            plant=plant,
        )
        env2 = json.loads(out2)
        self.assertEqual(exit_code2, 1, out2)
        self.assertEqual(env2["error"]["class"], "usage-error")
        self.assertIn(str(code_continue.MAX_CONTINUATION_ITERATION), env2["error"]["message"])


if __name__ == "__main__":
    unittest.main()
