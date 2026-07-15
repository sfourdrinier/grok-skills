# wrapper/scripts/tests/test_grokcli.py

import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

from groklib import GrokWrapperError, grokcli
from groklib import grokcli_output
from groklib import grokcli_probe
from groklib.authhome import PrivateHome
from groklib.progress import ProgressWriter, read_events
from groklib.sandbox import SandboxPolicy

_FAKE_BINARY = pathlib.Path(__file__).resolve().parent / "fake_grok.py"


class GrokCliTestBase(unittest.TestCase):
    """Shared fixtures: a fake-grok binary, a private home, a spec factory, a progress writer."""

    def setUp(self) -> None:
        self.scratch = pathlib.Path(tempfile.mkdtemp(prefix="grok-cli-grokcli-test-"))
        self.addCleanup(shutil.rmtree, str(self.scratch), True)

        self.cwd_dir = self.scratch / "cwd"
        self.cwd_dir.mkdir()

        self.home = self._make_home()

        self.prompt_file = self.scratch / "prompt.txt"
        self.prompt_file.write_text("Reply with exactly: PONG\n", encoding="utf-8")

        self.progress_path = self.scratch / "progress.jsonl"
        self.argv_log_path = self.scratch / "argv.log"

    def _make_home(self) -> PrivateHome:
        home_dir = self.scratch / "home"
        grok_dir = home_dir / ".grok"
        grok_dir.mkdir(parents=True)
        config_path = grok_dir / "config.toml"
        config_path.write_text("# config\n", encoding="utf-8")
        return PrivateHome(home_dir=home_dir, grok_dir=grok_dir, config_path=config_path)

    def _progress(self) -> ProgressWriter:
        return ProgressWriter("20260714T000000Z-abc123", self.progress_path)

    def _write_control(self, scenario: str, **extra: object) -> None:
        control = {"scenario": scenario, "argvLog": str(self.argv_log_path)}
        control.update(extra)
        control_path = self.home.home_dir / "fake-grok-control.json"
        control_path.write_text(json.dumps(control), encoding="utf-8")

    def _make_spec(self, **overrides: object) -> grokcli.GrokRunSpec:
        defaults = dict(
            binary=_FAKE_BINARY,
            cwd=self.cwd_dir,
            model="grok-4.5",
            prompt_file=self.prompt_file,
            output_schema=None,
            tools=("read_file", "list_dir", "grep"),
            allow_rules=(),
            sandbox=SandboxPolicy(
                mode="review", profile="read-only", writable_roots=(), secret_read_denial_proven=False
            ),
            permission_mode="auto",
            max_turns=30,
            timeout_seconds=30,
            leader_socket=self.home.grok_dir / "leader.sock",
            session_id="11111111-1111-4111-8111-111111111111",
            subagents_enabled=False,
            web_access=False,
            home=self.home,
        )
        defaults.update(overrides)
        return grokcli.GrokRunSpec(**defaults)

    def _read_argv_log(self) -> "list":
        lines = [line for line in self.argv_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines]


class BuildArgvTests(GrokCliTestBase):
    """build_argv emits exactly the C6 baseline: every flag once, prompt-file not positional."""

    def test_build_argv_contains_every_c6_flag_exactly_once(self) -> None:
        spec = self._make_spec()
        argv = grokcli.build_argv(spec)

        self.assertEqual(argv[0], str(spec.binary))

        required_flags = [
            "--prompt-file",
            "--verbatim",
            "--cwd",
            "--model",
            "--output-format",
            "--permission-mode",
            "--tools",
            "--no-subagents",
            "--no-memory",
            "--disable-web-search",
            "--no-plan",
            "--sandbox",
            "--max-turns",
            "--session-id",
            "--leader-socket",
        ]
        for flag in required_flags:
            self.assertEqual(argv.count(flag), 1, "flag {} must appear exactly once".format(flag))

        # prompt is delivered via --prompt-file; the positional / -p / --single
        # single-turn forms are never used.
        self.assertNotIn("-p", argv)
        self.assertNotIn("--single", argv)
        self.assertIn("--prompt-file", argv)
        prompt_index = argv.index("--prompt-file")
        self.assertEqual(argv[prompt_index + 1], str(spec.prompt_file))

        # No flag outside the C6 baseline set may appear.
        emitted_flags = [token for token in argv if token.startswith("--")]
        for flag in emitted_flags:
            self.assertIn(flag, grokcli.C6_BASELINE_FLAGS, "unexpected flag {}".format(flag))

    def test_build_argv_schema_run_adds_json_schema_alongside_streaming_json(self) -> None:
        # D-STREAM (T2-0): a schema run ALWAYS streams (--output-format
        # streaming-json) AND additionally emits --json-schema. The T2-0.0 live
        # probe proved the two flags compose and the terminal `end` event still
        # carries structuredOutput. (Pre-D-STREAM this test asserted
        # --output-format was absent for schema runs; that is the exact behavior
        # this task changed.)
        schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
        spec = self._make_spec(output_schema=schema)
        argv = grokcli.build_argv(spec)
        self.assertIn("--json-schema", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "streaming-json")
        schema_index = argv.index("--json-schema")
        self.assertEqual(json.loads(argv[schema_index + 1]), schema)

    def test_build_argv_always_streams(self) -> None:
        # Non-schema runs also stream: --output-format streaming-json, no
        # --json-schema.
        argv = grokcli.build_argv(self._make_spec())
        self.assertEqual(argv[argv.index("--output-format") + 1], "streaming-json")
        self.assertNotIn("--json-schema", argv)

    def test_build_argv_web_access_omits_disable_flag_and_adds_web_tools(self) -> None:
        base_tools = ("read_file", "list_dir", "grep")

        with_web = grokcli.build_argv(self._make_spec(tools=base_tools, web_access=True))
        self.assertNotIn("--disable-web-search", with_web)
        tools_index = with_web.index("--tools")
        with_web_tools = with_web[tools_index + 1].split(",")
        for web_tool in grokcli.WEB_TOOLS:
            self.assertIn(web_tool, with_web_tools)
        self.assertEqual(with_web_tools, list(base_tools) + list(grokcli.WEB_TOOLS))

        without_web = grokcli.build_argv(self._make_spec(tools=base_tools, web_access=False))
        self.assertIn("--disable-web-search", without_web)
        no_web_tools = without_web[without_web.index("--tools") + 1].split(",")
        for web_tool in grokcli.WEB_TOOLS:
            self.assertNotIn(web_tool, no_web_tools)

    def test_build_argv_empty_tools_uses_disallowed_tools(self) -> None:
        spec = self._make_spec(tools=(), web_access=False)
        argv = grokcli.build_argv(spec)
        self.assertNotIn("--tools", argv)
        self.assertIn("--disallowed-tools", argv)
        disallowed = argv[argv.index("--disallowed-tools") + 1].split(",")
        self.assertEqual(sorted(disallowed), sorted(grokcli.ALL_BUILTIN_TOOLS))

    def test_build_argv_subagents_enabled_omits_no_subagents(self) -> None:
        self.assertIn("--no-subagents", grokcli.build_argv(self._make_spec(subagents_enabled=False)))
        self.assertNotIn("--no-subagents", grokcli.build_argv(self._make_spec(subagents_enabled=True)))


class ChildEnvTests(GrokCliTestBase):
    """build_child_env is a minimal constructed dict, never an os.environ passthrough."""

    def test_child_env_is_minimal_and_home_is_private(self) -> None:
        spec = self._make_spec()
        with mock.patch.dict(os.environ, {"SECRET_SNEAKY": "should-not-appear"}, clear=False):
            env = grokcli.build_child_env(spec)
        self.assertEqual(set(env.keys()), {"HOME", "PATH", "TMPDIR"})
        self.assertEqual(env["HOME"], str(self.home.home_dir))
        self.assertNotIn("GROK_SANDBOX", env)
        self.assertNotIn("SECRET_SNEAKY", env)
        self.assertTrue(env["TMPDIR"].startswith(str(self.home.home_dir)))
        self.assertIn(str(spec.binary.parent), env["PATH"].split(os.pathsep))


class VersionTests(GrokCliTestBase):
    """check_version compares the first line of grok --version to the accepted pin, failing closed."""

    def test_check_version_matches_pin(self) -> None:
        returned = grokcli.check_version(_FAKE_BINARY)
        self.assertEqual(returned, grokcli.accepted_version())

    def test_version_mismatch_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_GROK_VERSION": "grok 9.9.9 (deadbeef) [fake]"}, clear=False):
            with self.assertRaises(GrokWrapperError) as caught:
                grokcli.check_version(_FAKE_BINARY)
        self.assertEqual(caught.exception.error_class, "version-mismatch")


class ExecuteTests(GrokCliTestBase):
    """execute spawns grok, enforces the timeout, and classifies every failure mode."""

    def test_execute_ok_json_parses_stop_reason_usage_session(self) -> None:
        self._write_control("ok-json")
        result = grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(result.stop_reason, "EndTurn")
        self.assertEqual(result.session_id, "c1c87161-4616-474d-8029-4b8b7e1ca9c2")
        self.assertEqual(result.request_id, "bcedfd69-64f8-4fcc-a2b4-acaa5706b70c")
        self.assertEqual(result.final_text, "PONG")
        self.assertEqual(result.exit_status, 0)
        self.assertIsInstance(result.model_usage, dict)
        self.assertEqual(result.effective_model, "grok-4.5")
        self.assertIsInstance(result.parsed.get("usage"), dict)

    def test_execute_ok_schema_returns_structured(self) -> None:
        schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
        self._write_control("ok-schema")
        result = grokcli.execute(self._make_spec(output_schema=schema), self._progress())
        self.assertEqual(result.structured, {"answer": "PONG"})

    def test_execute_no_stdout_classifies_output_missing(self) -> None:
        self._write_control("no-stdout")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-missing")

    def test_execute_malformed_json_classifies_output_malformed(self) -> None:
        self._write_control("malformed-json")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-malformed")

    def test_execute_partial_json_classifies_output_malformed(self) -> None:
        self._write_control("partial-json")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-malformed")

    def test_execute_schema_mismatch_classified(self) -> None:
        schema = {
            "type": "object",
            "required": ["answer", "count"],
            "properties": {"answer": {"type": "string"}, "count": {"type": "number"}},
        }
        self._write_control("ok-schema")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(output_schema=schema), self._progress())
        self.assertEqual(caught.exception.error_class, "schema-mismatch")
        self.assertEqual(caught.exception.detail.get("pointer"), "/count")

    def test_execute_cancelled_stop_reason_maps_to_cancelled_class(self) -> None:
        self._write_control("cancelled-stop")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "cancelled")

    def test_execute_turn_exhaustion_stop_reason_maps_to_turn_exhaustion_class(self) -> None:
        self._write_control("turn-exhaustion")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(max_turns=7), self._progress())
        self.assertEqual(caught.exception.error_class, "turn-exhaustion")

    def test_execute_nonzero_exit_classifies_cli_failure_with_stderr_captured(self) -> None:
        self._write_control("nonzero-exit")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "cli-failure")
        self.assertIn("fatal", str(caught.exception.detail.get("stderr", "")))

    def test_execute_timeout_kills_process_group_and_classifies_timeout(self) -> None:
        # The sleep-forever fake ignores SIGTERM and self-exits at 5s, so a
        # loose "well under 10s" bound would still pass if
        # platformsupport.kill_process_tree regressed to a no-op (execute
        # would then just idle through its own 5s reap timeout until the
        # fake self-exits around the 5s mark). Pinning elapsed < 3.0s -
        # strictly below that 5s self-exit - means only a real SIGKILL
        # delivered ahead of the self-exit can satisfy this test, so a
        # kill_process_tree regression is actually caught here.
        #
        # execute() raises GrokWrapperError on the timeout path rather than
        # returning a GrokRunResult, and the raised error's detail carries
        # only {"timeoutSeconds": ...} (see groklib/grokcli.py execute()) -
        # the reaped child's returncode/exit signal is not exposed through
        # this public result, so a direct signal-death assertion (negative
        # returncode = killed by signal on POSIX) is not available here and
        # the elapsed-time bound is the only strengthening applied.
        self._write_control("sleep-forever")
        start = time.monotonic()
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(timeout_seconds=1), self._progress())
        elapsed = time.monotonic() - start
        self.assertEqual(caught.exception.error_class, "timeout")
        self.assertLess(
            elapsed,
            3.0,
            "timeout path must kill the process tree well ahead of the fake's 5s self-exit",
        )

    def test_execute_timeout_does_not_hang_when_group_kill_is_swallowed(self) -> None:
        # round3 #9: platformsupport.kill_process_tree logs-and-swallows kill
        # failures. If it becomes a no-op (killpg EPERM / reparented tree), the
        # _on_timeout fallback (direct proc.kill + stdout.close to force EOF) must
        # STILL unblock the read loop so the wall-clock timeout returns instead of
        # the main thread blocking forever on readline. Pin elapsed < 3.0s -
        # strictly below the sleep-forever fake's 5s self-exit - so only the real
        # fallback (not the fake self-exiting) can satisfy the bound.
        self._write_control("sleep-forever")
        start = time.monotonic()
        with mock.patch.object(grokcli.platformsupport, "kill_process_tree", lambda proc: None):
            with self.assertRaises(GrokWrapperError) as caught:
                grokcli.execute(self._make_spec(timeout_seconds=1), self._progress())
        elapsed = time.monotonic() - start
        self.assertEqual(caught.exception.error_class, "timeout")
        self.assertLess(
            elapsed, 3.0, "the timeout fallback must not hang even when the group-kill is swallowed"
        )

    def test_sigterm_blocked_defers_signal_until_after_spawn_register(self) -> None:
        # Round4 F1-signal-race-orphan-grok-child: _sigterm_blocked must actually
        # defer SIGTERM delivery for the duration of the block, so a SIGTERM
        # arriving in the spawn->register window is not delivered until the child
        # is already registered (and thus killable).
        import os as _os
        import signal as _signal

        if not grokcli.platformsupport.is_posix() or not hasattr(_signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask is POSIX-only")

        delivered = {"during_block": False, "after_block": False}
        previous = _signal.getsignal(_signal.SIGTERM)

        def _handler(_signum, _frame):
            # Records whether the signal fired while still inside the block.
            delivered["after_block"] = True

        _signal.signal(_signal.SIGTERM, _handler)
        try:
            with grokcli._sigterm_blocked():
                _os.kill(_os.getpid(), _signal.SIGTERM)
                # The signal is pending but MUST NOT have been delivered yet.
                delivered["during_block"] = delivered["after_block"]
            # Give the just-unblocked pending signal a moment to be delivered.
            time.sleep(0.05)
        finally:
            _signal.signal(_signal.SIGTERM, previous)

        self.assertFalse(delivered["during_block"], "SIGTERM must be deferred inside the block")
        self.assertTrue(delivered["after_block"], "the deferred SIGTERM must fire after the block exits")

    def test_terminate_active_processes_kills_registered_child_tree(self) -> None:
        # The SIGTERM-handler teardown (grok_agent) relies on this: a registered
        # grok child (spawned in its own session) must be force-killed so a gate
        # termination cannot orphan it. Uses a real long-sleeping child.
        import subprocess as sp

        proc = sp.Popen(["sleep", "30"], **grokcli.platformsupport.spawn_kwargs_new_group())
        grokcli._register_active_proc(proc)
        try:
            count = grokcli.terminate_active_processes()
            self.assertEqual(count, 1)
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.returncode)
        finally:
            grokcli._unregister_active_proc(proc)
            if proc.poll() is None:
                proc.kill()

    def test_stderr_warnings_surface_as_warnings_with_valid_stdout(self) -> None:
        self._write_control("stderr-warn")
        result = grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(result.stop_reason, "EndTurn")
        self.assertIn("warning", result.stderr)

    def test_fake_received_exact_argv_from_build_argv(self) -> None:
        self._write_control("ok-json")
        spec = self._make_spec()
        grokcli.execute(spec, self._progress())
        logged = self._read_argv_log()
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["argv"], grokcli.build_argv(spec))
        self.assertEqual(logged[0]["HOME"], str(self.home.home_dir))
        self.assertFalse(logged[0]["GROK_SANDBOX_present"])


class StreamingExecuteTests(GrokCliTestBase):
    """D-STREAM (T2-0): execute relays streamed thought/text events to progress AND keeps the same envelope."""

    def _base_payload(self) -> dict:
        fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
        return json.loads((fixtures / "real-output-shape.json").read_text(encoding="utf-8"))

    def _read_progress(self) -> list:
        events, warnings = read_events(self.progress_path)
        self.assertEqual(warnings, [], "progress stream must be clean JSONL")
        return events

    def test_execute_relays_thought_then_text_progress_in_order(self) -> None:
        self._write_control("ok-json")
        grokcli.execute(self._make_spec(), self._progress())
        events = self._read_progress()
        # Seq is strictly monotonic (C3), phases all in the allowed set.
        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        # The streamed thought then text events were relayed as grok-phase events.
        stream_kinds = [
            event.get("data", {}).get("event")
            for event in events
            if event["phase"] == "grok" and isinstance(event.get("data"), dict) and "event" in event["data"]
        ]
        self.assertIn("thought", stream_kinds)
        self.assertIn("text", stream_kinds)
        self.assertLess(
            stream_kinds.index("thought"),
            stream_kinds.index("text"),
            "thought tokens stream before the answer text",
        )

    def test_execute_envelope_fields_equivalent_to_json_blob(self) -> None:
        # The assembled result carries the SAME envelope-relevant fields the
        # pre-D-STREAM `--output-format json` blob produced for the same run.
        self._write_control("ok-json")
        result = grokcli.execute(self._make_spec(), self._progress())
        blob = self._base_payload()
        blob_fields = grokcli_output.extract_result_fields(blob)
        self.assertEqual(result.stop_reason, blob_fields["stop_reason"])
        self.assertEqual(result.session_id, blob_fields["session_id"])
        self.assertEqual(result.request_id, blob_fields["request_id"])
        self.assertEqual(result.model_usage, blob_fields["model_usage"])
        self.assertEqual(result.effective_model, blob_fields["effective_model"])
        self.assertEqual(result.final_text, blob_fields["final_text"])
        self.assertEqual(result.structured, blob_fields["structured"])
        # usage + num_turns (read from result.parsed by the envelope builder).
        self.assertEqual(result.parsed.get("usage"), blob.get("usage"))
        self.assertEqual(result.parsed.get("num_turns"), blob.get("num_turns"))

    def test_execute_stream_no_terminal_classifies_output_malformed(self) -> None:
        # A stream of all-valid thought/text lines that never emits a terminal
        # `end` event is a torn stream: fail closed as output-malformed.
        self._write_control("stream-no-terminal")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-malformed")
        self.assertEqual(caught.exception.detail.get("reason"), "no-terminal-event")

    def test_execute_invalid_utf8_byte_classifies_output_malformed(self) -> None:
        # F2 (round 2): a non-UTF-8 byte on grok's stdout makes the strict-UTF-8
        # readline() raise UnicodeDecodeError. The wrapper must catch it and fail
        # closed as output-malformed, producing the real run's GrokWrapperError
        # (NOT an uncaught exception that escapes classification and orphans the
        # run).
        self._write_control("invalid-utf8")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-malformed")
        self.assertEqual(caught.exception.detail.get("reason"), "unparseable-stream-line")

    def test_execute_unparseable_stream_line_classifies_output_malformed(self) -> None:
        # A PARTIAL stream (a valid thought THEN a bad line) is not misclassified:
        # saw_any_line is True, malformed is True -> output-malformed.
        self._write_control("malformed-json")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-malformed")
        self.assertEqual(caught.exception.detail.get("reason"), "unparseable-stream-line")

    def test_execute_fully_malformed_stream_classifies_output_malformed_not_missing(self) -> None:
        # F-STREAM-MALFORMED: a FULLY-malformed stream (only non-JSON lines, exit
        # 0) never reaches the assembler, so saw_any_line stays False while
        # malformed is True. It must classify output-malformed, NOT output-missing
        # -- there WAS output; it was unparseable. The malformed check runs before
        # the empty-stream branch.
        self._write_control("malformed-only")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.execute(self._make_spec(), self._progress())
        self.assertEqual(caught.exception.error_class, "output-malformed")
        self.assertEqual(caught.exception.detail.get("reason"), "unparseable-stream-line")

    def test_execute_progress_write_failure_degrades_and_run_completes(self) -> None:
        # F-STREAM-OSERR: a progress.jsonl append failure on the per-event relay
        # emits must DEGRADE (drop further relay events) without crashing the run.
        # The stdout read loop keeps feeding the assembler, so execute() still
        # returns a complete GrokRunResult and the envelope is still produced.
        self._write_control("ok-json")

        class _RelayFailingProgress(ProgressWriter):
            """Raises OSError on the per-event stream relay emits ('grok streamed ...'),
            while letting the lifecycle emits through, to simulate a mid-stream
            progress.jsonl write failure."""

            def emit(self, phase, message, level="info", data=None):
                if message.startswith("grok streamed"):
                    raise OSError("simulated progress.jsonl append failure")
                return super().emit(phase, message, level=level, data=data)

        failing = _RelayFailingProgress("20260714T000000Z-abc123", self.progress_path)
        result = grokcli.execute(self._make_spec(), failing)
        # The run completed cleanly despite the relay write failures.
        self.assertIsInstance(result, grokcli.GrokRunResult)
        blob_fields = grokcli_output.extract_result_fields(self._base_payload())
        self.assertEqual(result.stop_reason, blob_fields["stop_reason"])
        self.assertEqual(result.final_text, blob_fields["final_text"])


class InspectAndProbeTests(GrokCliTestBase):
    """inspect_home parses the config surface; probe_login parses login state and models."""

    def test_inspect_home_parses_config_surface(self) -> None:
        self._write_control("inspect-ok")
        result = grokcli_probe.inspect_home(_FAKE_BINARY, self.home, self.home.grok_dir / "leader.sock")
        for key in ("permissions", "hooks", "plugins", "mcpServers", "configSources"):
            self.assertIn(key, result)
        self.assertEqual(result["grokVersion"], "0.2.101")

    def test_probe_login_parses_login_and_models(self) -> None:
        self._write_control("models-ok")
        result = grokcli_probe.probe_login(_FAKE_BINARY, self.home, self.home.grok_dir / "leader.sock")
        self.assertTrue(result["loggedIn"])
        self.assertEqual(result["defaultModel"], "grok-4.5")
        self.assertIn("grok-4.5", result["models"])

    def test_probe_login_not_logged_in_raises_auth_missing(self) -> None:
        self._write_control("models-loggedout")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_probe.probe_login(_FAKE_BINARY, self.home, self.home.grok_dir / "leader.sock")
        self.assertEqual(caught.exception.error_class, "auth-missing")

    def test_inspect_home_nonzero_exit_is_cli_failure_even_when_parseable(self) -> None:
        # S3: cancelled-stop emits VALID JSON but exits nonzero; a nonzero probe
        # exit must fail closed (cli-failure) rather than be treated as success
        # just because the output happens to parse.
        self._write_control("cancelled-stop")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_probe.inspect_home(_FAKE_BINARY, self.home, self.home.grok_dir / "leader.sock")
        self.assertEqual(caught.exception.error_class, "cli-failure")

    def test_probe_login_nonzero_exit_is_cli_failure_even_when_parseable(self) -> None:
        # S3: same for probe_login -- a nonzero `grok models` exit fails closed.
        self._write_control("cancelled-stop")
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_probe.probe_login(_FAKE_BINARY, self.home, self.home.grok_dir / "leader.sock")
        self.assertEqual(caught.exception.error_class, "cli-failure")

    def test_probe_timeout_kills_process_tree(self) -> None:
        # S3: a hung probe must be force-killed as a WHOLE process tree. The
        # sleep-forever fake ignores SIGTERM and self-exits at 5s, so pinning
        # elapsed < 3.0s means only a real process-group SIGKILL (delivered ahead
        # of the self-exit) can satisfy this -- a group-kill regression is caught.
        self._write_control("sleep-forever")
        start = time.monotonic()
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli_probe._run_read_only_probe(
                _FAKE_BINARY, self.home, ["inspect", "--json"], 1, "inspect"
            )
        elapsed = time.monotonic() - start
        self.assertEqual(caught.exception.error_class, "timeout")
        self.assertLess(
            elapsed,
            3.0,
            "probe timeout must kill the process tree well ahead of the fake's 5s self-exit",
        )


if __name__ == "__main__":
    unittest.main()
