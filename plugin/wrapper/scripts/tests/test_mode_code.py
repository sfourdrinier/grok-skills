# wrapper/scripts/tests/test_mode_code.py

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


def _plant_sentinel_in_worktree(worktree_path: pathlib.Path, run_id: str) -> None:
    (worktree_path / (".grok-run-" + run_id)).write_text("", encoding="utf-8")


class CodeModeTests(WorktreeModeHarness):
    """code runs Grok write-capable inside an isolated worktree, gating the result end to end."""

    def _run(self, repo, extra_argv=None, **kwargs):
        argv = ["code", "--target", "pkg", "--base", "HEAD", "--task", "Fix the module"]
        if extra_argv:
            argv = argv[:1] + extra_argv + argv[1:]
        return self.drive(argv, repo_root=repo, **kwargs)

    def test_code_fails_closed_on_uncommitted_dependency(self) -> None:
        repo = self.make_code_repo()
        (repo / "newpkg").mkdir()  # exists on disk but is NOT committed in the base
        exit_code, out = self.drive(
            ["code", "--target", "newpkg", "--base", "HEAD", "--task", "Work"], repo_root=repo
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "worktree-failure")

    def test_code_creates_and_verifies_external_worktree_before_grok_runs(self) -> None:
        repo = self.make_code_repo()
        order = []

        def _record(name, func):
            def _wrapped(*args, **kwargs):
                order.append(name)
                return func(*args, **kwargs)

            return _wrapped

        patchers = [
            mock.patch.object(
                worktree_mod, "create_external_worktree", _record("create", worktree_mod.create_external_worktree)
            ),
            mock.patch.object(
                worktree_mod, "verify_external_worktree", _record("verify", worktree_mod.verify_external_worktree)
            ),
            mock.patch.object(grokcli, "execute", _record("execute", grokcli.execute)),
        ]
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree, extra_patchers=patchers)
        self.assertEqual(exit_code, 0, out)
        self.assertIn("create", order)
        self.assertIn("verify", order)
        self.assertIn("execute", order)
        self.assertLess(order.index("create"), order.index("verify"))
        self.assertLess(order.index("verify"), order.index("execute"))

    def test_code_never_touches_env_files(self) -> None:
        repo = self.make_code_repo()
        (repo / "pkg" / ".env").write_text("SECRET=shhh\n", encoding="utf-8")  # uncommitted operator secret
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        worktree_path = pathlib.Path(env["worktreePath"])
        planted = worktree_path / "pkg" / ".env"
        self.assertFalse(planted.exists(), "an uncommitted .env must never appear in the worktree")
        self.assertFalse(planted.is_symlink(), ".env must never be symlinked into the worktree")

    def test_code_unexpected_edit_outside_worktree_fails(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self._run(
            repo,
            scenario="writes-outside",
            control_extra={"editTarget": str(repo / "a.txt")},
            plant=_plant_sentinel_in_worktree,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")
        # The original checkout is left untouched by the wrapper's own logic; the
        # failure is the SANDBOX-escape edit the fake made, detected post-run.
        self.assertIn(str(repo / "a.txt"), json.dumps(env["error"]["detail"]))

    def test_code_in_execute_model_failure_carries_completed_result(self) -> None:
        # F1-execute-and-verify-drops-result (worktree path): a model-unavailable
        # failure fires INSIDE _execute_and_verify, right after grok produced its
        # answer. The completed (redacted) answer must still ride on the failure
        # envelope via the result holder, not be dropped to None.
        repo = self.make_code_repo()
        exit_code, out = self._run(repo, scenario="model-mismatch", plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "model-unavailable")
        self.assertIsNotNone(env.get("response"))
        self.assertEqual(env["response"]["text"], "PONG")
        self.assertIsNotNone(env.get("grok"))

    def test_code_sigterm_after_completed_result_carries_answer(self) -> None:
        # F1-sigterm-drops-result (worktree path): a SIGTERM/BaseException escaping
        # the lifecycle AFTER Grok produced a completed answer must carry the redacted
        # grok/response onto the terminal "cancelled" envelope via the holder, not
        # drop it. Injected by letting _execute_and_verify populate the result holder,
        # then raising SystemExit before the body returns.
        repo = self.make_code_repo()
        real_execute_and_verify = _shared._execute_and_verify

        def _ev_then_sigterm(*args, **kwargs):
            real_execute_and_verify(*args, **kwargs)  # populates the passed result holder
            raise SystemExit(143)

        exit_code, out = self._run(
            repo,
            plant=_plant_sentinel_in_worktree,
            extra_patchers=[mock.patch.object(_worktree, "_execute_and_verify", _ev_then_sigterm)],
        )
        non_empty = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1, "exactly one envelope must reach stdout")
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "cancelled")
        self.assertIsNotNone(env.get("response"))
        self.assertEqual(env["response"]["text"], "PONG")
        self.assertIsNotNone(env.get("grok"))

    def test_code_worktree_retained_on_success(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["cleanup"]["status"], "retained")
        self.assertEqual(env["cleanup"]["detail"], env["worktreePath"])
        self.assertTrue(pathlib.Path(env["worktreePath"]).is_dir(), "worktree must survive a successful run")

    def test_code_validation_command_failure_fails_run(self) -> None:
        repo = self.make_code_repo(build_script=True)
        exit_code, out = self._run(
            repo, plant=_plant_sentinel_in_worktree, env_extra={"FAKE_PNPM_GATE_EXIT": "1"}
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "validation-failure")
        gate_records = [c for c in env["commands"] if c["purpose"] == "build-gate:build"]
        self.assertEqual(len(gate_records), 1)
        self.assertEqual(gate_records[0]["exitStatus"], 1)

    def test_code_build_gate_pins_to_target_dir_despite_rename(self) -> None:
        # build-gate location pinning: Grok renaming the target package.json name
        # (e.g. to ANOTHER existing workspace's name) must NOT redirect the build
        # gate onto a different package. The gate runs with cwd set to the IMMUTABLE
        # target DIRECTORY and classifies by the ORIGINAL committed name, so a
        # rename cannot redirect it.
        repo = self.make_code_repo(build_script=True)

        def _rename_and_plant(worktree_path: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(worktree_path, run_id)
            (worktree_path / "pkg" / "package.json").write_text(
                json.dumps({"name": "@some/other-workspace", "scripts": {"build": "true"}}),
                encoding="utf-8",
            )

        exit_code, out = self._run(repo, plant=_rename_and_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        gate = [c for c in env["commands"] if c["purpose"] == "build-gate:build"]
        self.assertEqual(len(gate), 1, env["commands"])
        record = gate[0]
        # The gate ran the detected package manager's run command...
        self.assertEqual(record["argv"][1:], ["run", "build"])
        # ...pinned to the target directory by CWD, never by the renamed name.
        self.assertEqual(pathlib.Path(record["cwd"]).name, "pkg")
        self.assertNotIn("@some/other-workspace", json.dumps(record))

    def test_code_root_target_build_gate_pins_by_dir_despite_rename(self) -> None:
        # build-gate location pinning: a repo-root target (--target .) gates in the
        # ROOT directory. If Grok renames the root package.json, the gate cwd (the
        # worktree root) cannot be redirected, so the gate still runs against the
        # intended package.
        repo = self.make_code_repo(build_script=True)
        (repo / "package.json").write_text(
            json.dumps({"name": "root-under-test", "scripts": {"build": "true"}}),
            encoding="utf-8",
        )
        self._git(repo, "add", "package.json")
        self._git(repo, "commit", "-q", "-m", "add root package.json")

        def _rename_root_and_plant(worktree_path: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(worktree_path, run_id)
            (worktree_path / "package.json").write_text(
                json.dumps({"name": "@some/other-workspace", "scripts": {"build": "true"}}),
                encoding="utf-8",
            )

        exit_code, out = self.drive(
            ["code", "--target", ".", "--base", "HEAD", "--task", "Fix the repo"],
            repo_root=repo,
            plant=_rename_root_and_plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        gate = [c for c in env["commands"] if c["purpose"] == "build-gate:build"]
        self.assertEqual(len(gate), 1, env["commands"])
        record = gate[0]
        self.assertEqual(record["argv"][1:], ["run", "build"])
        # cwd is the worktree root itself (the repo-root target).
        self.assertEqual(pathlib.Path(record["cwd"]), pathlib.Path(env["worktreePath"]))
        self.assertNotIn("@some/other-workspace", json.dumps(record))
        self.assertNotIn("root-under-test", json.dumps(record))

    def test_code_full_build_gate_commands_recorded(self) -> None:
        build_repo = self.make_code_repo(build_script=True)
        exit_code, out = self._run(build_repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        purposes = [c["purpose"] for c in env["commands"]]
        self.assertIn("build-gate:build", purposes)
        self.assertNotIn("build-gate:typecheck", purposes)

        scriptless_repo = self.make_code_repo(build_script=False)
        exit_code, out = self._run(scriptless_repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        purposes = [c["purpose"] for c in env["commands"]]
        self.assertIn("build-gate:typecheck", purposes)
        self.assertIn("build-gate:lint", purposes)
        self.assertNotIn("build-gate:build", purposes)

    def test_code_build_gate_skipped_when_grok_changes_gate_script(self) -> None:
        # D1(b): if Grok CHANGES a gate script definition during the run, the
        # wrapper refuses to execute it and the run fails (validation-failure).
        repo = self.make_code_repo(build_script=True)  # base build script is "true"

        def _change_build_and_plant(worktree_path: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(worktree_path, run_id)
            (worktree_path / "pkg" / "package.json").write_text(
                json.dumps({"name": "pkg-under-test", "scripts": {"build": "echo grok-rewrote-this"}}),
                encoding="utf-8",
            )

        exit_code, out = self._run(repo, plant=_change_build_and_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "validation-failure")
        self.assertIn("gate-scripts-modified", env["error"]["message"])
        purposes = [c["purpose"] for c in env["commands"]]
        self.assertNotIn("build-gate:build", purposes, "a modified gate script must never be executed")

    def test_code_build_gate_skipped_when_grok_adds_gate_script(self) -> None:
        # D1(b): if Grok ADDS a build script the base commit did not have, the
        # gate would now execute Grok-authored code -- it is refused fail-closed.
        repo = self.make_code_repo(build_script=False)  # base has typecheck + lint, no build

        def _add_build_and_plant(worktree_path: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(worktree_path, run_id)
            (worktree_path / "pkg" / "package.json").write_text(
                json.dumps(
                    {"name": "pkg-under-test", "scripts": {"typecheck": "true", "lint": "true", "build": "echo added"}}
                ),
                encoding="utf-8",
            )

        exit_code, out = self._run(repo, plant=_add_build_and_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "validation-failure")
        self.assertIn("gate-scripts-modified", env["error"]["message"])
        purposes = [c["purpose"] for c in env["commands"]]
        self.assertFalse(
            any(p.startswith("build-gate:") for p in purposes),
            "an added gate script must cause the whole gate to be refused",
        )

    def test_code_build_gate_runs_when_gate_scripts_unmodified(self) -> None:
        # D1(b) negative: a run that leaves the gate script definitions untouched
        # executes the gate exactly as before, with no gate-scripts-modified warning.
        repo = self.make_code_repo(build_script=True)
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        purposes = [c["purpose"] for c in env["commands"]]
        self.assertIn("build-gate:build", purposes)
        self.assertFalse(
            any("gate-scripts-modified" in warning for warning in env["warnings"]),
            env["warnings"],
        )

    def test_code_never_build_workspaces_skip_build_via_project_config(self) -> None:
        # ProjectConfig mechanism: a repo can pin NEVER-build workspaces in a
        # .grok-skills.json at its root. A pinned workspace runs exactly the
        # configured validation list instead of building, even when a build script
        # exists -- proving the never-build map is project config, not hardcoded.
        cases = (
            ("@acme/schemas", ["typecheck"], {"build-gate:typecheck"}, {"build-gate:build", "build-gate:lint"}),
            ("@acme/ui", ["typecheck", "lint"], {"build-gate:typecheck", "build-gate:lint"}, {"build-gate:build"}),
        )
        for name, pinned_scripts, expected, forbidden in cases:
            with self.subTest(workspace=name):
                repo = self.make_code_repo(build_script=True)
                manifest = {"name": name, "scripts": {"build": "true", "typecheck": "true", "lint": "true"}}
                (repo / "pkg" / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
                self._git(repo, "add", "pkg/package.json")
                self._git(repo, "commit", "-q", "-m", "never-build workspace manifest")
                (repo / ".grok-skills.json").write_text(
                    json.dumps({"neverBuildWorkspaces": {name: pinned_scripts}}), encoding="utf-8"
                )
                exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
                env = json.loads(out)
                self.assertEqual(exit_code, 0, out)
                purposes = {
                    c["purpose"] for c in env["commands"] if c["purpose"].startswith("build-gate:")
                }
                self.assertTrue(expected.issubset(purposes), purposes)
                self.assertEqual(purposes & forbidden, set(), purposes)

    def test_code_sentinel_in_worktree_populates_effective_working_directory(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["effectiveWorkingDirectory"], env["worktreePath"])

    def test_code_missing_sentinel_is_wrong_working_directory(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self._run(repo, plant=None)  # Grok never created the cwd sentinel
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "wrong-working-directory")

    def test_code_misplaced_sentinel_is_wrong_working_directory(self) -> None:
        repo = self.make_code_repo()

        def _plant_in_checkout(worktree_path: pathlib.Path, run_id: str) -> None:
            (repo / (".grok-run-" + run_id)).write_text("", encoding="utf-8")

        exit_code, out = self._run(repo, plant=_plant_in_checkout)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "wrong-working-directory")

    def test_code_sandbox_policy_is_write_confinement(self) -> None:
        # D-SECRETREAD: policy_for_mode never raises probe-required for code; it
        # returns a valid workspace write-confinement policy.
        policy = sandbox.policy_for_mode(
            "code",
            worktree=pathlib.Path("/tmp/some-worktree").resolve(),
            private_tmp=pathlib.Path("/tmp").resolve(),
        )
        # The DISTINCT custom profile extends the workspace built-in (Grok
        # dogfood-2 #6), never shadows it.
        self.assertEqual(policy.profile, "grok-skills-code")
        self.assertFalse(policy.secret_read_denial_proven)

        repo = self.make_code_repo()
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["sandbox"]["reportedProfile"], "grok-skills-code")
        self.assertTrue(env["sandbox"]["enforced"])

    def test_code_binds_write_confinement_to_run_private_tmp_not_os_tempdir(self) -> None:
        # Grok dogfood-2 #4: the write-confinement policy actually used for
        # verification must bind to the RUN-PRIVATE tmp (<home>/tmp), never the
        # whole OS temp dir. Spy on policy_for_mode's private_tmp: the exec policy
        # (built after the home exists) must point inside a gs-* private home.
        repo = self.make_code_repo()
        real_policy_for_mode = _worktree.policy_for_mode
        seen_private_tmps = []

        def _spy(mode, *, worktree, private_tmp):
            seen_private_tmps.append(str(private_tmp))
            return real_policy_for_mode(mode, worktree=worktree, private_tmp=private_tmp)

        exit_code, out = self._run(
            repo,
            plant=_plant_sentinel_in_worktree,
            extra_patchers=[mock.patch.object(_worktree, "policy_for_mode", _spy)],
        )
        self.assertEqual(exit_code, 0, out)
        # The LAST (exec) policy_for_mode call binds the narrow run-private tmp.
        exec_private_tmp = seen_private_tmps[-1]
        self.assertIn(runstate.TEMP_HOME_PREFIX, exec_private_tmp, exec_private_tmp)
        self.assertTrue(exec_private_tmp.endswith("/tmp"), exec_private_tmp)

    def test_code_unprobed_platform_blocks_before_worktree_and_spawn(self) -> None:
        # SEC1: an unprobed platform fails closed with probe-required BEFORE the
        # worktree or private home is created and BEFORE Grok is spawned.
        repo = self.make_code_repo()
        worktrees_before = self._worktree_dirs()
        homes_before = self.temp_home_prefix_dirs()
        with mock.patch.object(platformsupport, "current_platform", lambda: "linux"):
            exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "probe-required")
        self.assertEqual(self._worktree_dirs() - worktrees_before, set())
        self.assertEqual(self.temp_home_prefix_dirs() - homes_before, set())
        self.assertFalse(self.argv_log_path.exists(), "grok must not spawn on an unprobed platform")

    def test_code_preexisting_original_tracked_edit_is_tolerated(self) -> None:
        # F1: the operator's PRE-EXISTING uncommitted tracked work in the original
        # checkout must NOT be misread as a run-introduced escape. A run touching
        # only the worktree succeeds even though a tracked file already diverges.
        repo = self.make_code_repo()
        (repo / "a.txt").write_text("alpha\nbeta\noperator-work-in-progress\n", encoding="utf-8")
        exit_code, out = self._run(repo, plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")

    def test_code_new_original_edit_flagged_despite_preexisting(self) -> None:
        # F1: over a checkout that already carries a pre-existing operator edit
        # (a.txt), a NEW tracked edit the run causes in the original checkout
        # (pkg/mod.txt) is still flagged, while the pre-existing edit is tolerated.
        repo = self.make_code_repo()
        (repo / "a.txt").write_text("alpha\nbeta\noperator-work-in-progress\n", encoding="utf-8")
        exit_code, out = self._run(
            repo,
            scenario="writes-outside",
            control_extra={"editTarget": str(repo / "pkg" / "mod.txt")},
            plant=_plant_sentinel_in_worktree,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")
        detail = json.dumps(env["error"]["detail"])
        self.assertIn(str((repo / "pkg" / "mod.txt").resolve()), detail)
        self.assertNotIn(str((repo / "a.txt").resolve()), detail)

    def test_code_build_gate_write_into_original_checkout_flagged(self) -> None:
        # PR968 codex post-build-gate: the build gate runs Grok-modifiable pnpm
        # scripts in the UNSANDBOXED wrapper process AFTER the entry escape scan
        # already passed. A build step that writes into the operator's REAL checkout
        # must still be flagged fail-closed by the post-gate original-checkout re-scan.
        repo = self.make_code_repo(build_script=True)
        escape_target = repo / "build-gate-escaped.txt"
        malicious_pnpm = pathlib.Path(self.tmp_root) / "malicious_pnpm.sh"
        malicious_pnpm.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  install) exit 0 ;;\n"
            '  *) printf escaped > "$GROK_ESCAPE_TARGET"; exit 0 ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        os.chmod(str(malicious_pnpm), 0o755)
        exit_code, out = self._run(
            repo,
            plant=_plant_sentinel_in_worktree,
            env_extra={
                "GROK_PACKAGE_MANAGER_BINARY": str(malicious_pnpm),
                "GROK_ESCAPE_TARGET": str(escape_target),
            },
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-edits")
        detail = env["error"]["detail"]
        self.assertEqual(detail.get("phase"), "post-build-gate")
        self.assertTrue(any(str(escape_target.resolve()) in v for v in detail["violations"]), detail)

    def test_code_oserror_after_worktree_records_terminal_runjson(self) -> None:
        # F3: a non-classified failure (OSError) after the worktree exists must
        # leave run.json terminal WITH worktreePath set so cleanup can rebuild +
        # reap the physical worktree, rather than a "running"/worktreePath=None
        # record cleanup cannot find.
        repo = self.make_code_repo()

        def _boom(*args, **kwargs):
            raise OSError("simulated prompt-file write failure")

        exit_code, out = self._run(
            repo,
            plant=_plant_sentinel_in_worktree,
            extra_patchers=[mock.patch.object(_worktree, "_write_prompt_file", _boom)],
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "cli-failure")

        runs_dir = runstate.state_root() / "runs"
        records = [
            json.loads((entry / "run.json").read_text(encoding="utf-8"))
            for entry in runs_dir.iterdir()
            if (entry / "run.json").is_file()
        ]
        code_records = [record for record in records if record.get("mode") == "code"]
        self.assertEqual(len(code_records), 1, code_records)
        self.assertEqual(code_records[0]["status"], "failure")
        self.assertIsNotNone(code_records[0]["worktreePath"], "cleanup needs the worktree path")

    def test_code_failed_auth_teardown_is_fail_closed_not_silent(self) -> None:
        # S4 / Grok dogfood-2 #2: a FAILED private-home teardown is FAIL-CLOSED --
        # a non-success cleanup-failure outcome with an honest warning, never a
        # silent exit-0. code still retains its WORKTREE (cleanup.status
        # "retained"), but the failed auth teardown flips the run to a failure.
        repo = self.make_code_repo()
        real_destroy = _envelope.destroy_private_home
        calls = {"count": 0}

        def _failed_destroy(home):
            calls["count"] += 1
            real_destroy(home)  # actually remove it so the private home never leaks
            return {"status": "failed", "detail": "simulated teardown failure"}

        exit_code, out = self._run(
            repo,
            plant=_plant_sentinel_in_worktree,
            extra_patchers=[mock.patch.object(_envelope, "destroy_private_home", _failed_destroy)],
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "cleanup-failure")
        self.assertEqual(env["cleanup"]["status"], "retained")
        self.assertGreaterEqual(calls["count"], 2, "teardown must be retried once before giving up")
        self.assertTrue(
            any("teardown reported failed" in warning for warning in env["warnings"]),
            env["warnings"],
        )

    def test_code_success_envelope_validates_and_records_web_policy(self) -> None:
        repo = self.make_code_repo()
        exit_code, out = self._run(repo, extra_argv=["--web"], plant=_plant_sentinel_in_worktree)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["mode"], "code")
        self.assertEqual(envelope_mod.validate_envelope(env), [])
        self.assertTrue(env["policy"]["webAccess"])
        argv = self.read_run_argv()
        self.assertNotIn("--disable-web-search", argv)
        tools = self.flag_value(argv, "--tools").split(",")
        self.assertIn("search_replace", tools)
        self.assertIn("run_terminal_command", tools)


    def test_code_writes_handoff_artifacts_and_step_order(self) -> None:
        """PR4: successful code writes handoff JSON + patch; steps include locked order."""
        repo = self.make_code_repo()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            target = wt / "pkg" / "impl.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("implemented\n", encoding="utf-8")

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        run_id = env["runId"]
        run_dir = runstate.state_root() / "runs" / run_id
        manifest_path = run_dir / "implementation-handoff.json"
        patch_path = run_dir / "artifacts" / "implementation.patch"
        self.assertTrue(manifest_path.is_file(), "missing implementation-handoff.json")
        self.assertTrue(patch_path.is_file(), "missing implementation.patch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        from groklib.implementation_handoff import validate_implementation_handoff, _STEP_ORDER
        self.assertEqual(validate_implementation_handoff(manifest), [])
        self.assertIn("impl.txt", " ".join(c.get("path", "") for c in manifest.get("changedFiles", [])))
        # handoff mode dual-condition
        code_h, out_h = self.drive(["handoff", "--run-id", run_id], repo_root=repo)
        env_h = json.loads(out_h)
        self.assertEqual(code_h, 0, out_h)
        self.assertTrue(env_h["response"]["integration"]["ready"])

    def test_code_contract_scope_violation_fails(self) -> None:
        repo = self.make_code_repo()
        contract = {
            "schemaVersion": 1,
            "taskId": "scope-test",
            "target": "pkg",
            "writeScopes": [{"kind": "file", "path": "pkg/only-this.ts"}],
            "requiredValidation": [],
        }
        cpath = pathlib.Path(self.tmp_root) / "contract.json"
        cpath.write_text(json.dumps(contract), encoding="utf-8")

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "outside.ts").write_text("x\n", encoding="utf-8")

        exit_code, out = self._run(
            repo,
            extra_argv=["--contract-file", str(cpath)],
            plant=plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "write-scope-violation")
        run_id = env["runId"]
        manifest_path = runstate.state_root() / "runs" / run_id / "implementation-handoff.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["integration"]["ready"])

    def test_code_unexpected_commit_no_reset_forensics(self) -> None:
        """HEAD moved after Grok → unexpected-commit; worktree preserved; handoff written."""
        import subprocess

        repo = self.make_code_repo()
        base_sha = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()

        def plant(wt: pathlib.Path, run_id: str) -> None:
            _plant_sentinel_in_worktree(wt, run_id)
            (wt / "pkg" / "moved.txt").write_text("payload\n", encoding="utf-8")
            # Simulate an unexpected commit on the worktree branch (policy violation).
            subprocess.run(
                ["git", "-C", str(wt), "add", "pkg/moved.txt"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(wt),
                    "-c",
                    "user.email=test@example.com",
                    "-c",
                    "user.name=test",
                    "commit",
                    "-q",
                    "-m",
                    "unexpected",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            head_after = subprocess.check_output(
                ["git", "-C", str(wt), "rev-parse", "HEAD"], text=True
            ).strip()
            self.assertNotEqual(head_after, base_sha)

        exit_code, out = self._run(repo, plant=plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "unexpected-commit")
        run_id = env["runId"]
        run_dir = runstate.state_root() / "runs" / run_id
        manifest_path = run_dir / "implementation-handoff.json"
        self.assertTrue(manifest_path.is_file(), "forensic handoff must still be written")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["integration"]["ready"])
        kinds = [b.get("kind") for b in manifest["integration"].get("blockers") or []]
        self.assertIn("unexpected-commit", kinds)
        # Worktree retained; HEAD not reset to base
        wt_path = pathlib.Path(env["worktreePath"] or "")
        self.assertTrue(wt_path.is_dir(), env)
        head_now = subprocess.check_output(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True
        ).strip()
        self.assertNotEqual(head_now, base_sha, "must not reset worktree HEAD after unexpected-commit")
        self.assertEqual(env["cleanup"]["status"], "retained")


class CwdSentinelTests(unittest.TestCase):
    """Grok r3 #12: the cwd sentinel must be a real regular file, not a spoofable symlink/dir."""

    def _worktree(self):
        import shutil
        import tempfile

        from groklib.worktree import ExternalWorktree

        wt_dir = pathlib.Path(tempfile.mkdtemp(prefix="grok-sentinel-wt-"))
        repo_dir = pathlib.Path(tempfile.mkdtemp(prefix="grok-sentinel-repo-"))
        self.addCleanup(shutil.rmtree, str(wt_dir), True)
        self.addCleanup(shutil.rmtree, str(repo_dir), True)
        return ExternalWorktree(path=wt_dir, branch="grok/code/x", base_revision="deadbeef", repo_root=repo_dir)

    def test_symlink_sentinel_is_rejected_and_real_file_accepted(self) -> None:
        from groklib import GrokWrapperError
        from groklib.modes import code as code_mod

        wt = self._worktree()
        name = ".grok-run-20260101T000000Z-abcdef"

        # A symlink standing in for the sentinel must NOT satisfy the check.
        target = wt.path / "elsewhere.txt"
        target.write_text("x", encoding="utf-8")
        (wt.path / name).symlink_to(target)
        with self.assertRaises(GrokWrapperError) as ctx:
            code_mod._assert_cwd_sentinel(wt, name)
        self.assertEqual(ctx.exception.error_class, "wrong-working-directory")

        # A directory named like the sentinel is also rejected.
        (wt.path / name).unlink()
        (wt.path / name).mkdir()
        with self.assertRaises(GrokWrapperError):
            code_mod._assert_cwd_sentinel(wt, name)

        # A real, regular-file sentinel passes.
        (wt.path / name).rmdir()
        (wt.path / name).write_text("", encoding="utf-8")
        code_mod._assert_cwd_sentinel(wt, name)  # no raise

        # A sentinel ALSO present in the original checkout is rejected (lexists
        # catches even a symlink planted there).
        (wt.repo_root / name).symlink_to(target)
        with self.assertRaises(GrokWrapperError) as ctx2:
            code_mod._assert_cwd_sentinel(wt, name)
        self.assertEqual(ctx2.exception.error_class, "wrong-working-directory")


if __name__ == "__main__":
    unittest.main()
