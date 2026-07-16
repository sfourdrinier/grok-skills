# wrapper/scripts/tests/test_mode_review.py

import json
import pathlib
import shutil
import tempfile
import types
import unittest
from unittest import mock

from groklib import GrokWrapperError
from groklib import envelope as envelope_mod
from groklib import platformsupport, runstate
from groklib.authhome import create_private_home
from groklib.modes import _envelope, _shared
from groklib.progress import ProgressWriter

from tests import gitfixtures
from tests.modefixtures import ModeHarness, make_review_repo


def _run_record_for(state_home, run_id):
    run_dir = pathlib.Path(state_home) / "grok-skills" / "runs" / run_id
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


class ReviewModeTests(ModeHarness):
    """review runs Grok read-only over a repo workspace with the full C7 rules payload."""

    def _repo(self) -> pathlib.Path:
        return make_review_repo(pathlib.Path(self.tmp_root))

    def test_review_prompt_file_contains_rules_then_task_in_c7_order(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review the module for defects"],
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        prompt = self.read_prompt(argv)

        rules_index = prompt.index("=== REPOSITORY RULES (governing; read completely before the task) ===")
        begin_index = prompt.index("--- BEGIN AGENTS.md ---")
        task_index = prompt.index("=== TASK ===")
        answer_index = prompt.index("Review the module for defects")
        self.assertLess(rules_index, begin_index)
        self.assertLess(begin_index, task_index)
        self.assertLess(task_index, answer_index)
        self.assertIn("Always read the rules before acting.", prompt)

    def test_review_tools_flag_exactly_read_grep_listdir(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        tools = self.flag_value(argv, "--tools")
        self.assertIsNotNone(tools)
        self.assertEqual(tools.split(","), ["read_file", "grep", "list_dir"])

    def test_review_cwd_is_target_workspace(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        cwd = self.flag_value(argv, "--cwd")
        self.assertEqual(pathlib.Path(cwd).resolve(), (repo / "pkg").resolve())

    def test_review_invalid_target_fails_closed(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "does-not-exist", "--task", "Review"], repo_root=repo
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "invalid-target")

    def test_review_survives_persistent_progress_write_oserror(self) -> None:
        # F1 (round 2): a progress.jsonl write that raises OSError on EVERY emit
        # must NOT crash the real run. safe_emit degrades; the REAL run still
        # produces its own success envelope + run record under the REAL run id --
        # no synthesized run id from grok_agent.main's generic handler, and no
        # run left orphaned in "running" with no envelope.json.
        from groklib.progress import ProgressWriter

        def _always_oserror(self, *args, **kwargs):
            raise OSError("simulated persistent progress-write failure (disk full)")

        repo = self._repo()
        with mock.patch.object(ProgressWriter, "emit", _always_oserror):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        run_id = envelope["runId"]
        run_dir = pathlib.Path(self.state_home) / "grok-skills" / "runs" / run_id
        self.assertTrue((run_dir / "envelope.json").is_file(), "the REAL run's envelope.json must exist")
        run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run_record["status"], "success", "the REAL run's record must be terminal, not orphaned")

    def test_review_oserror_on_prompt_write_terminalizes_real_run(self) -> None:
        # round3 F2-shared: a non-GrokWrapperError (OSError from _write_prompt_file)
        # must terminalize the REAL run -- run.json failure under the SAME run id
        # the envelope carries -- not a synthesized id with run.json stuck at
        # "running".
        repo = self._repo()

        def _boom(*args, **kwargs):
            raise OSError("simulated prompt-file write failure")

        with mock.patch.object(_shared, "_write_prompt_file", _boom):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "cli-failure")

        runs_dir = pathlib.Path(self.state_home) / "grok-skills" / "runs"
        review_records = [
            json.loads((entry / "run.json").read_text(encoding="utf-8"))
            for entry in runs_dir.iterdir()
            if (entry / "run.json").is_file()
        ]
        review_records = [record for record in review_records if record.get("mode") == "review"]
        self.assertEqual(len(review_records), 1, review_records)
        self.assertEqual(review_records[0]["status"], "failure")
        self.assertEqual(review_records[0]["runId"], env["runId"], "the envelope must carry the REAL run id")

    def test_create_run_midfailure_terminalizes_under_real_run_id(self) -> None:
        # Round4 F1-create-run-outside-try: a failure PARTWAY through create_run
        # (after the run dir + owner marker exist) must terminalize the REAL run --
        # the emitted envelope carries the real run id and a terminal run.json
        # exists under it -- never a synthesized, dangling id with an orphan dir.
        repo = self._repo()

        def _boom(_run_id):
            raise RuntimeError("simulated post-marker create_run failure")

        with mock.patch.object(runstate, "emit_run_id_marker", _boom):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        run_id = env["runId"]
        # The REAL run dir exists and carries a terminal run.json under that id.
        record = _run_record_for(self.state_home, run_id)
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["runId"], run_id, "the envelope must carry the REAL run id")

    def test_finalize_failure_fail_closed_not_success(self) -> None:
        # Durable terminal publish failure must not exit 0 as success.
        repo = self._repo()

        def _fail_finalize(*_a, **_k):
            raise OSError("simulated terminal finalize failure (disk full)")

        with mock.patch(
            "groklib.modes.finalize_worker.run_finalize_parent",
            side_effect=_fail_finalize,
        ), mock.patch.object(_shared, "run_finalize_parent", side_effect=_fail_finalize):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "finalization-worker-missing-result")

    def test_sigterm_systemexit_midrun_terminalizes_and_emits_one_envelope(self) -> None:
        # Round4 F5-sigterm-bypasses-envelope: a SIGTERM-driven SystemExit
        # (BaseException, not Exception) mid-run must still terminalize the run --
        # tear down the private home, write a terminal run.json, and emit EXACTLY
        # ONE C4 envelope -- instead of exiting with run.json stuck at "running"
        # and nothing on stdout.
        repo = self._repo()
        before = self.temp_home_prefix_dirs()

        def _sigterm(*args, **kwargs):
            raise SystemExit(143)

        with mock.patch.object(_shared, "_execute_and_verify", _sigterm):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        non_empty = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1, "exactly one envelope must reach stdout")
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "cancelled")
        record = _run_record_for(self.state_home, env["runId"])
        self.assertEqual(record["status"], "failure", "run.json must be terminal, not stuck at running")
        self.assertEqual(
            record.get("lifecycle"),
            "canceled",
            "operator cancel must durable-terminalize as lifecycle canceled",
        )
        self.assertEqual(
            self.temp_home_prefix_dirs() - before, set(), "the private home must be torn down on SIGTERM"
        )

    def test_sigterm_after_completed_result_carries_answer(self) -> None:
        # F1-sigterm-drops-result: a SIGTERM/BaseException escaping the lifecycle
        # AFTER Grok produced a completed answer (here: raised during the post-run
        # no-repo-writes assertion, after _execute_and_verify populated the result
        # holder) must still carry the redacted grok/response onto the terminal
        # "cancelled" envelope, not drop it and force recovery from the unredacted
        # progress.jsonl.
        repo = self._repo()

        def _sigterm(*args, **kwargs):
            raise SystemExit(143)

        with mock.patch.object(_shared, "_report_repo_fs_drift", _sigterm):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        non_empty = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1, "exactly one envelope must reach stdout")
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "cancelled")
        # The completed answer rides on the TERMINAL envelope (redacted response).
        self.assertIsNotNone(env.get("response"))
        self.assertEqual(env["response"]["text"], "PONG")
        self.assertIsNotNone(env.get("grok"))

    def test_review_requested_model_family_boundary_rejects_cross_family(self) -> None:
        # Grok dogfood #4: requesting grok-4 must NOT accept the grok-4.5 the CLI
        # actually ran (a raw startswith would). Fail closed as model-unavailable.
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review", "--model", "grok-4"], repo_root=repo
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "model-unavailable")

    def test_terminalize_helper_drops_secret_bearing_detail_in_fallback(self) -> None:
        # The terminalizer builds a failure envelope under the REAL run id; if the
        # classified detail carries secret-shaped material the fail-closed scanner
        # would reject, it falls back to a minimal detail-free envelope under the
        # SAME id, so exactly one (secret-free) envelope still reaches the caller.
        run_paths = runstate.create_run("review")
        progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        exc = GrokWrapperError(
            "cli-failure", "grok exited nonzero", {"stderr": "Authorization: Bearer abc123def456ABCDEF"}
        )
        env = _shared.terminalize_unexpected_failure(
            run_paths=run_paths,
            mode="review",
            progress=progress,
            exc=exc,
            write_terminal_record=lambda: None,
            log=lambda *args: None,
        )
        self.assertEqual(env["runId"], run_paths.run_id)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "cli-failure")
        self.assertNotIn("abc123def456ABCDEF", json.dumps(env), "no secret leaks in the fallback envelope")

    def test_review_invalid_target_escaping_repo_fails_closed(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "../outside", "--task", "Review"], repo_root=repo
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["error"]["class"], "invalid-target")

    def test_review_reports_instruction_hashes_in_envelope(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        instructions = envelope["instructions"]
        self.assertEqual(len(instructions), 1)
        entry = instructions[0]
        self.assertEqual(entry["path"], "AGENTS.md")
        self.assertGreater(entry["bytes"], 0)
        self.assertEqual(len(entry["sha256"]), 64)

    def test_review_filesystem_write_is_warn_not_failure(self) -> None:
        # Concurrent FS drift (or even a sneaky cwd write) must NOT discard a
        # completed review. Product: warn-only; hard-fail only when Grok reports edits.
        repo = gitfixtures.make_repo(pathlib.Path(self.tmp_root))
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"],
            scenario="writes-into-cwd",
            repo_root=repo,
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        warnings = envelope.get("warnings") or []
        self.assertTrue(
            any("Files changed during this review" in str(w) for w in warnings),
            warnings,
        )
        self.assertIsNotNone(envelope.get("response"))

    def test_review_checkout_mutation_does_not_override_real_failure(self) -> None:
        # Malformed output stays the failure class; FS drift is only a note.
        repo = gitfixtures.make_repo(pathlib.Path(self.tmp_root))
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"],
            scenario="writes-into-cwd-then-malformed",
            repo_root=repo,
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "output-malformed")
        warnings = envelope.get("warnings") or []
        self.assertTrue(
            any("Files changed during this review" in str(w) for w in warnings),
            warnings,
        )

    def test_review_fs_baseline_capture_failure_does_not_block_review(self) -> None:
        # Baseline is informational only: git glitches must not block a review.
        repo = self._repo()
        call_count = {"n": 0}

        def _boom(_repo_root):
            call_count["n"] += 1
            # Fail the pre-run capture only; post-run drift check may still run.
            if call_count["n"] == 1:
                raise GrokWrapperError(
                    "worktree-failure", "simulated git fingerprint failure", {"reason": "index-lock"}
                )
            return frozenset()

        with mock.patch.object(_shared.worktree_escape, "repo_change_fingerprint", _boom):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        warnings = envelope.get("warnings") or []
        self.assertTrue(
            any("Could not snapshot the tree before this review" in str(w) for w in warnings),
            warnings,
        )

    def test_review_fs_drift_success_still_carries_completed_result(self) -> None:
        # FS drift after a completed Grok answer: success envelope keeps the body.
        repo = gitfixtures.make_repo(pathlib.Path(self.tmp_root))
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"],
            scenario="writes-into-cwd",
            repo_root=repo,
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertIsNotNone(envelope.get("response"))
        self.assertEqual(envelope["response"]["text"], "PONG")
        self.assertIsNotNone(envelope.get("grok"))

    def test_review_secret_in_model_output_is_redacted_and_reported(self) -> None:
        # Round4 / dogfood-2 #3: a review that legitimately quotes a secret in its
        # answer is redact-and-REPORTED (success, body preserved, secret masked),
        # not hard-failed into a generic validation-failure that loses the body.
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"],
            scenario="secret-in-output",
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        envelope = json.loads(out)
        self.assertEqual(envelope["status"], "success")
        text = envelope["response"]["text"]
        self.assertNotIn("sk-ABCDEF0123456789ABCDEFGHIJKLMNOP", text)
        self.assertIn("[redacted-api-key-token]", text)
        # The rest of the body is preserved (not dropped).
        self.assertIn("rotate it", text)

    def test_review_file_change_in_output_is_informational(self) -> None:
        # Grok listing change-shaped JSON keys must not discard the review body.
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"],
            scenario="reports-file-change",
            repo_root=repo,
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertIsNotNone(envelope.get("response"))
        self.assertEqual(envelope["response"]["text"], "PONG")
        self.assertIsNotNone(envelope.get("grok"))
        warnings = envelope.get("warnings") or []
        self.assertTrue(
            any("file-change fields" in str(w) for w in warnings),
            warnings,
        )

    def test_review_schema_passthrough_sets_json_schema_flag(self) -> None:
        repo = self._repo()
        schema_path = pathlib.Path(self.tmp_root) / "schema.json"
        schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review", "--schema", str(schema_path)],
            scenario="ok-schema",
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        self.assertIn("--json-schema", argv)
        # D-STREAM (T2-0): schema runs now ALSO stream; --output-format
        # streaming-json is present alongside --json-schema.
        self.assertEqual(self.flag_value(argv, "--output-format"), "streaming-json")
        self.assertEqual(json.loads(self.flag_value(argv, "--json-schema")), schema)

    def test_success_envelope_validates_and_has_progress_path(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope_mod.validate_envelope(envelope), [])
        self.assertIsNotNone(envelope["progressStreamPath"])
        self.assertTrue(envelope["progressStreamPath"].endswith("progress.jsonl"))
        self.assertEqual(envelope["effectiveModel"], "grok-4.5")
        self.assertEqual(envelope["sandbox"]["reportedProfile"], "grok-skills-review")
        self.assertTrue(envelope["sandbox"]["enforced"])

    def test_web_flag_enables_web_tools_and_records_policy(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review", "--web"], repo_root=repo
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertTrue(envelope["policy"]["webAccess"])
        argv = self.read_run_argv()
        self.assertNotIn("--disable-web-search", argv)
        tools = self.flag_value(argv, "--tools").split(",")
        for web_tool in ("web_search", "web_fetch", "open_page", "open_page_with_find"):
            self.assertIn(web_tool, tools)

    def test_default_run_has_web_disabled(self) -> None:
        repo = self._repo()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertFalse(envelope["policy"]["webAccess"])
        argv = self.read_run_argv()
        self.assertIn("--disable-web-search", argv)

    def test_private_home_destroyed_on_failure(self) -> None:
        repo = self._repo()
        before = self.temp_home_prefix_dirs()
        exit_code, out = self.drive(
            ["review", "--target", "pkg", "--task", "Review"],
            scenario="nonzero-exit",
            repo_root=repo,
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "cli-failure")
        self.assertEqual(envelope["cleanup"]["status"], "clean")
        after = self.temp_home_prefix_dirs()
        self.assertEqual(after - before, set(), "the run's private home must be destroyed on failure")

    def test_unprobed_platform_blocks_before_any_spawn(self) -> None:
        # SEC1: an unprobed platform must fail closed with probe-required BEFORE
        # any private home is created or Grok is ever spawned -- never run to
        # completion with an unverified sandbox and trip the gate only post-run.
        repo = self._repo()
        before = self.temp_home_prefix_dirs()
        with mock.patch.object(platformsupport, "current_platform", lambda: "linux"):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "probe-required")
        # No private home was created and Grok never spawned (no argv log).
        self.assertEqual(self.temp_home_prefix_dirs() - before, set())
        self.assertFalse(self.argv_log_path.exists(), "grok must not spawn on an unprobed platform")

    def test_failed_auth_teardown_is_fail_closed_not_silent(self) -> None:
        # S4 / Grok dogfood-2 #2: a FAILED private-home teardown is FAIL-CLOSED --
        # a non-success outcome (cleanup-failure) with an honest cleanup.status and
        # a warning, never a silent exit-0-as-if-clean. Teardown is retried once
        # (here it fails both times) before giving up.
        repo = self._repo()
        real_destroy = _envelope.destroy_private_home
        calls = {"count": 0}

        def _failed_destroy(home):
            calls["count"] += 1
            real_destroy(home)  # actually remove it so the private home never leaks
            return {"status": "failed", "detail": "simulated teardown failure"}

        with mock.patch.object(_envelope, "destroy_private_home", _failed_destroy):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "cleanup-failure")
        self.assertEqual(envelope["cleanup"]["status"], "failed")
        self.assertGreaterEqual(calls["count"], 2, "teardown must be retried once before giving up")
        self.assertTrue(
            any("teardown reported failed" in warning for warning in envelope["warnings"]),
            envelope["warnings"],
        )

    def test_failed_teardown_on_raw_exception_path_is_fail_closed(self) -> None:
        # Round5 cleanup-outcome-lost-on-terminalize: a raw (non-GrokWrapperError)
        # exception escaping AFTER the private home exists still runs the inner
        # teardown. If that teardown FAILED (auth copy may remain), the terminalized
        # envelope must surface it FAIL-CLOSED (cleanup.status "failed"), never the
        # default "not-applicable" that hid the leaked auth copy before.
        repo = self._repo()
        real_destroy = _envelope.destroy_private_home

        def _failed_destroy(home):
            real_destroy(home)  # actually remove it so nothing genuinely leaks
            return {"status": "failed", "detail": "simulated teardown failure"}

        def _boom(*args, **kwargs):
            raise OSError("simulated prompt-file write failure after home creation")

        with mock.patch.object(_envelope, "destroy_private_home", _failed_destroy), mock.patch.object(
            _shared, "_write_prompt_file", _boom
        ):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        # The raw OSError terminalizes as cli-failure, but the FAILED teardown is
        # surfaced fail-closed in the cleanup field rather than not-applicable.
        self.assertEqual(envelope["cleanup"]["status"], "failed")

    def test_failed_then_clean_teardown_retry_recovers_to_success(self) -> None:
        # The retry upgrades a transient first-failure to success: the run stays a
        # success and cleanup.status reports clean once the retry confirms removal.
        repo = self._repo()
        real_destroy = _envelope.destroy_private_home
        calls = {"count": 0}

        def _flaky_destroy(home):
            calls["count"] += 1
            if calls["count"] == 1:
                # Transient failure that removed nothing; the home is still on disk.
                return {"status": "failed", "detail": "transient first-attempt failure"}
            return real_destroy(home)  # the retry actually removes it, cleanly

        with mock.patch.object(_envelope, "destroy_private_home", _flaky_destroy):
            exit_code, out = self.drive(
                ["review", "--target", "pkg", "--task", "Review"], repo_root=repo
            )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["cleanup"]["status"], "clean")
        self.assertEqual(calls["count"], 2, "the retry must have run exactly once after the first failure")

    def test_finally_does_not_double_destroy_or_clobber_clean_status(self) -> None:
        home = create_private_home(
            source_grok_dir=self.source_grok,
            auth_file_names=("auth.json",),
            config_toml="# config\n",
        )
        cleanup = _shared.HomeCleanup(home)
        first = cleanup.destroy_once()
        second = cleanup.destroy_once()
        self.assertEqual(first["status"], "clean")
        self.assertEqual(second["status"], "clean")
        self.assertIs(first, second)
        self.assertFalse(home.home_dir.exists())


class _RecordingProgress:
    """Minimal ProgressWriter stand-in that records safe_emit calls (no I/O)."""

    def __init__(self) -> None:
        self.emitted = []

    def safe_emit(self, *args, **kwargs) -> None:
        self.emitted.append((args, kwargs))


class ReportRepoFsDriftTests(unittest.TestCase):
    """FS drift during review is warn-only (never unexpected-edits)."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-norepowrites-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.repo = gitfixtures.make_repo(pathlib.Path(self.tmp_root))
        # Make the committed tracked file dirty so it is part of the entry baseline
        # alongside the fixture's pre-existing untracked dirty.txt.
        with (self.repo / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("operator local edit\n")

    def _run(self):
        return types.SimpleNamespace(repository=str(self.repo))

    def test_restored_and_deleted_pre_existing_dirt_are_warned(self) -> None:
        baseline = _shared.worktree_escape.repo_change_fingerprint(self.repo)
        # The run restores the dirty tracked file back to HEAD and deletes the
        # pre-existing untracked file: both baseline entries VANISH from `after`.
        gitfixtures._git(self.repo, ["checkout", "--", "a.txt"])
        (self.repo / "dirty.txt").unlink()

        warnings: list = []
        progress = _RecordingProgress()
        _shared._report_repo_fs_drift(self._run(), baseline, progress, warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("a.txt", warnings[0])
        self.assertIn("dirty.txt", warnings[0])
        self.assertIn("Files changed during this review", warnings[0])
        self.assertIn("informational only", warnings[0])

    def test_untouched_pre_existing_dirt_passes(self) -> None:
        # Control: when the run leaves the operator's pre-existing dirt exactly as
        # captured, the check passes and records its clean signal (no false flag on
        # baseline dirt in either diff direction).
        baseline = _shared.worktree_escape.repo_change_fingerprint(self.repo)
        progress = _RecordingProgress()
        warnings: list = []
        _shared._report_repo_fs_drift(self._run(), baseline, progress, warnings)
        self.assertEqual(warnings, [])
        self.assertEqual(len(progress.emitted), 1)


if __name__ == "__main__":
    unittest.main()
