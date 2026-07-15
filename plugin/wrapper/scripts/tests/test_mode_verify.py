# wrapper/scripts/tests/test_mode_verify.py

import json
import pathlib
import unittest

from groklib import envelope as envelope_mod
from groklib import runstate

from tests.worktreefixtures import WorktreeModeHarness

_VALID_VERDICT = json.dumps({"verdict": "pass", "evidence": ["ran the build", "ran the tests"]})


class VerifyModeTests(WorktreeModeHarness):
    """verify runs Grok read-only over an existing worktree, extracting a schema-constrained verdict."""

    def _verify_argv(self, worktree_path: pathlib.Path):
        return ["verify", "--worktree", str(worktree_path), "--task", "Confirm the change is correct"]

    def test_verify_requires_existing_registered_worktree(self) -> None:
        repo = self.make_code_repo()
        # pkg is a directory inside the main checkout, but NOT a registered worktree.
        exit_code, out = self.drive(self._verify_argv(repo / "pkg"), repo_root=repo)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "worktree-failure")

    def test_verify_verdict_schema_enforced_and_extracted(self) -> None:
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["verifier"]["verdict"], "pass")

        # A structurally-invalid verdict (value outside the enum) is enforced by
        # the wrapper and classified verifier-unavailable, not silently accepted.
        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": json.dumps({"verdict": "maybe", "evidence": []})},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "verifier-unavailable")

    def test_verify_missing_verdict_is_verifier_unavailable(self) -> None:
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        exit_code, out = self.drive(
            self._verify_argv(worktree.path), repo_root=repo, scenario="verifier-missing"
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "verifier-unavailable")

    def test_verify_source_edit_detected_fails(self) -> None:
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)

        def _edit_tracked_source(worktree_path: pathlib.Path, run_id: str) -> None:
            (worktree_path / "a.txt").write_text("verifier edited a tracked source file\n", encoding="utf-8")

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
            plant=_edit_tracked_source,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")

    def test_verify_gitignored_artifact_dir_changes_pass(self) -> None:
        # PR968 codex verify-artifact-ignore: a build output under dist/ is
        # tolerated ONLY because git GENUINELY ignores it (committed .gitignore),
        # not merely because it sits under a dist-named dir.
        repo = self.make_code_repo()
        (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore")
        self._git(repo, "commit", "-q", "-m", "ignore dist output")
        worktree = self.make_registered_worktree(repo)

        def _write_build_artifact(worktree_path: pathlib.Path, run_id: str) -> None:
            dist = worktree_path / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "out.js").write_text("compiled output\n", encoding="utf-8")

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
            plant=_write_build_artifact,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["verifier"]["verdict"], "pass")

    def test_verify_tracked_file_under_build_dir_flagged(self) -> None:
        # PR968 codex verify-artifact-ignore: a TRACKED source file under a
        # top-level build/ dir (git does NOT ignore it) modified during verify is
        # an unexpected edit -- never tolerated merely for sitting under "build/".
        repo = self.make_code_repo()
        (repo / "build").mkdir(parents=True, exist_ok=True)
        (repo / "build" / "orchestrate.ts").write_text("export const v = 1\n", encoding="utf-8")
        self._git(repo, "add", "build/orchestrate.ts")
        self._git(repo, "commit", "-q", "-m", "track build/orchestrate.ts source")
        worktree = self.make_registered_worktree(repo)

        def _edit_tracked_build_file(worktree_path: pathlib.Path, run_id: str) -> None:
            (worktree_path / "build" / "orchestrate.ts").write_text("export const v = 2\n", encoding="utf-8")

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
            plant=_edit_tracked_build_file,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")

    def test_verify_tolerates_prior_code_uncommitted_edits(self) -> None:
        # The real code->verify handoff: verify adopts the SAME worktree a prior
        # code run produced, and that worktree still carries the code run's
        # UNCOMMITTED implementation edits (new files + tracked modifications).
        # verify must NOT misattribute those pre-existing edits to itself; its
        # change-confinement base is a working-tree snapshot taken at verify
        # entry, not the worktree HEAD.
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        (worktree.path / "slugify.py").write_text(
            "def slugify(value):\n    return value.strip().lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        (worktree.path / "a.txt").write_text("alpha\nbeta\ncode-run-edit\n", encoding="utf-8")

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["verifier"]["verdict"], "pass")
        # verify itself changed nothing, so its own change set is empty.
        self.assertEqual(env["changedFiles"], [])

    def test_verify_flags_only_new_edits_over_prior_code_edits(self) -> None:
        # Over a worktree already carrying a prior code run's uncommitted edit,
        # verify must still fail closed on a NEW edit it causes during the run,
        # while the pre-existing code edit is tolerated. This proves the snapshot
        # base flags exactly the verify-caused delta and nothing else.
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        (worktree.path / "slugify.py").write_text(
            "def slugify(value):\n    return value\n", encoding="utf-8"
        )

        def _verify_makes_new_edit(worktree_path: pathlib.Path, run_id: str) -> None:
            (worktree_path / "rogue.py").write_text("print('verify wrote this')\n", encoding="utf-8")

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
            plant=_verify_makes_new_edit,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")
        detail = json.dumps(env["error"]["detail"])
        self.assertIn("rogue.py", detail)
        self.assertNotIn("slugify.py", detail)

    def test_verify_tolerates_preexisting_original_tracked_edit(self) -> None:
        # F1: the operator's PRE-EXISTING uncommitted tracked work in the ORIGINAL
        # checkout must NOT be misattributed to verify. A verify run that changes
        # nothing succeeds even though a tracked file already diverges upstream.
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        (repo / "a.txt").write_text("alpha\nbeta\noperator-work-in-progress\n", encoding="utf-8")
        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["verifier"]["verdict"], "pass")

    def test_verify_new_original_edit_flagged_despite_preexisting(self) -> None:
        # F1: over a checkout that already carries a pre-existing operator edit
        # (a.txt), a NEW tracked edit in the original checkout during the run
        # (pkg/mod.txt) is still flagged unexpected-edits, while the pre-existing
        # edit is tolerated.
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        (repo / "a.txt").write_text("alpha\nbeta\noperator-work-in-progress\n", encoding="utf-8")
        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="writes-outside",
            control_extra={"editTarget": str(repo / "pkg" / "mod.txt")},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")
        detail = json.dumps(env["error"]["detail"])
        self.assertIn(str((repo / "pkg" / "mod.txt").resolve()), detail)
        self.assertNotIn(str((repo / "a.txt").resolve()), detail)

    def test_verify_envelope_includes_verifier_identity(self) -> None:
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope_mod.validate_envelope(env), [])
        self.assertEqual(env["verifier"]["identity"], "grok-grok-4.5")
        self.assertEqual(env["mode"], "verify")

    def test_verify_does_not_enroll_adopted_worktree_for_its_own_cleanup(self) -> None:
        # PR968 codex verify-adopted-worktree: a verify run ADOPTS the worktree a
        # prior code run created and OWNS. The verify run must NOT record that
        # borrowed worktree into its OWN cleanup record -- otherwise cleanup of the
        # verify run rebuilds it and calls remove_external_worktree with the verify
        # run id, but the worktree path + sibling marker name the ORIGINAL code
        # run, so the run-id binding fails and cleanup of the verify run wedges
        # (and, worse, would target the code run's live worktree). The owning code
        # run stays responsible for reaping it.
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        code_run_id = worktree.path.name

        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        verify_run_id = env["runId"]
        self.assertNotEqual(verify_run_id, code_run_id)
        # The verify envelope still reports which worktree it inspected...
        self.assertEqual(
            pathlib.Path(env["worktreePath"]).resolve(), worktree.path.resolve()
        )

        # ...but the verify run's cleanup record carries NO worktree, so cleanup of
        # the verify run cannot rebuild or reap the borrowed worktree.
        record = runstate.load_run_record(verify_run_id)
        self.assertIsNone(record["worktreePath"])
        self.assertIsNone(record["worktreeBranch"])
        self.assertIsNone(record["baseRevision"])

        # Cleaning up the verify run succeeds (no wedge) and leaves the code run's
        # worktree fully intact.
        cleanup_exit, cleanup_out = self.drive(
            ["cleanup", "--run-id", verify_run_id, "--confirm"],
            repo_root=repo,
        )
        cleanup_env = json.loads(cleanup_out)
        self.assertEqual(cleanup_exit, 0, cleanup_out)
        self.assertEqual(cleanup_env["status"], "success", cleanup_out)
        self.assertFalse(cleanup_env["response"]["worktreeRemoved"], cleanup_out)
        self.assertTrue(worktree.path.is_dir(), "the code run's worktree must survive verify cleanup")

    def test_verify_is_hermetic_no_web(self) -> None:
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertFalse(env["policy"]["webAccess"])
        argv = self.read_run_argv()
        self.assertIn("--disable-web-search", argv)
        tools = self.flag_value(argv, "--tools").split(",")
        self.assertNotIn("search_replace", tools)
        self.assertNotIn("write", tools)
        self.assertIn("run_terminal_command", tools)

    def test_verify_elicits_verdict_schema_via_json_schema(self) -> None:
        # verify must send its wrapper-owned verdict schema to the CLI as
        # --json-schema so the model reliably returns a structured verdict; a
        # prompt-only schema left the live model free to omit it. The schema is
        # elicit-only: grokcli does NOT own the missing/invalid classification
        # (that stays verify's verifier-unavailable), which the missing-verdict
        # test above continues to prove.
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        exit_code, out = self.drive(
            self._verify_argv(worktree.path),
            repo_root=repo,
            scenario="ok-schema",
            control_extra={"structured": _VALID_VERDICT},
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        self.assertIn("--json-schema", argv)
        # D-STREAM (T2-0): verify (elicit_schema) now streams too; --output-format
        # streaming-json rides alongside --json-schema and the terminal `end`
        # event carries structuredOutput (T2-0.0 probe).
        self.assertEqual(self.flag_value(argv, "--output-format"), "streaming-json")
        sent_schema = json.loads(self.flag_value(argv, "--json-schema"))
        self.assertEqual(sent_schema.get("required"), ["verdict", "evidence"])
        self.assertEqual(sent_schema["properties"]["verdict"]["enum"], ["pass", "fail", "inconclusive"])

    def test_verify_web_flag_is_rejected(self) -> None:
        repo = self.make_code_repo()
        worktree = self.make_registered_worktree(repo)
        argv = self._verify_argv(worktree.path) + ["--web"]
        exit_code, out = self.drive(argv, repo_root=repo)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "usage-error")


if __name__ == "__main__":
    unittest.main()
