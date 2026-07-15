# wrapper/scripts/tests/test_entrypoint.py

import contextlib
import io
import json
import signal
import unittest
from unittest import mock

import grok_agent
from groklib import envelope as envelope_mod, platformsupport

from tests.preflightfixtures import PreflightHarness


def _run_main(argv):
    """Run grok_agent.main(argv), returning (exit_code, stdout_text)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = grok_agent.main(argv)
    return exit_code, buffer.getvalue()


class SigtermHandlerTests(unittest.TestCase):
    """The wrapper's SIGTERM handler tears down running grok children, then exits cleanly."""

    def test_sigterm_handler_kills_active_procs_then_exits_143(self) -> None:
        if not platformsupport.is_posix():
            self.skipTest("the SIGTERM handler is POSIX-only")
        previous = signal.getsignal(signal.SIGTERM)
        try:
            grok_agent._install_sigterm_handler()
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler), "a SIGTERM handler must be installed on POSIX")

            killed = {"count": 0}

            def _fake_terminate() -> int:
                killed["count"] += 1
                return 3

            with mock.patch.object(grok_agent.grokcli, "terminate_active_processes", _fake_terminate):
                with self.assertRaises(SystemExit) as ctx:
                    handler(signal.SIGTERM, None)
            self.assertEqual(killed["count"], 1, "the handler must tear down active grok children")
            self.assertEqual(ctx.exception.code, 143)
        finally:
            signal.signal(signal.SIGTERM, previous)


class BaseExceptionDispatchTests(unittest.TestCase):
    """F5: a BaseException (SIGTERM SystemExit / Ctrl-C) escaping dispatch still emits one envelope."""

    def test_systemexit_in_dispatch_terminalizes_as_cancelled_envelope(self) -> None:
        # F5-baseexception-during-earliest-create-run-window: a mode runner that
        # re-raises a SIGTERM-driven SystemExit(143) (fired before a run dir
        # existed) must be caught by main() and turned into exactly ONE cancelled
        # C4 envelope, never escape past sys.exit(main()) with empty stdout.
        def _raiser(_args):
            raise SystemExit(143)

        with mock.patch.dict(grok_agent.MODES, {"preflight": _raiser}):
            exit_code, out = _run_main(["preflight"])
        envelope = json.loads(out)  # the whole capture parses as one JSON document
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "cancelled")

    def test_keyboardinterrupt_in_dispatch_terminalizes_as_cancelled(self) -> None:
        def _raiser(_args):
            raise KeyboardInterrupt()

        with mock.patch.dict(grok_agent.MODES, {"preflight": _raiser}):
            exit_code, out = _run_main(["preflight"])
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "cancelled")


class UsageErrorTests(unittest.TestCase):
    """Argparse failures become usage-error envelopes, never a bare exit 2 or traceback."""

    def test_unknown_subcommand_emits_usage_error_envelope_exit_1(self) -> None:
        exit_code, out = _run_main(["banana"])
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "usage-error")
        self.assertIn(envelope["mode"], envelope_mod.MODES)

    def test_argparse_error_is_captured_as_usage_error_envelope(self) -> None:
        # status requires --run-id; omitting it is an argparse error.
        exit_code, out = _run_main(["status"])
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["error"]["class"], "usage-error")
        self.assertEqual(envelope["mode"], "status")

    def test_non_positive_timeout_and_max_turns_are_usage_errors(self) -> None:
        # Grok r3 #11 timeout-max-turns-not-validated: a non-positive or absurd
        # --timeout / --max-turns is rejected fail-closed at argv parse as a
        # usage-error, never accepted (which would burn a private-home cycle).
        for argv in (
            ["review", "--target", "pkg", "--task", "x", "--timeout", "0"],
            ["review", "--target", "pkg", "--task", "x", "--timeout", "-5"],
            ["code", "--target", "pkg", "--base", "HEAD", "--task", "x", "--max-turns", "0"],
            ["code", "--target", "pkg", "--base", "HEAD", "--task", "x", "--max-turns", "-1"],
            ["review", "--target", "pkg", "--task", "x", "--timeout", "99999999999"],
        ):
            with self.subTest(argv=argv):
                exit_code, out = _run_main(argv)
                envelope = json.loads(out)
                self.assertEqual(exit_code, 1, out)
                self.assertEqual(envelope["status"], "failure")
                self.assertEqual(envelope["error"]["class"], "usage-error")

    def test_valid_timeout_and_max_turns_are_accepted(self) -> None:
        # A normal positive budget must NOT be rejected by the new bound.
        parser = grok_agent._build_parser()
        args = parser.parse_args(["review", "--target", "pkg", "--task", "x", "--timeout", "1200", "--max-turns", "40"])
        self.assertEqual(args.timeout, 1200)
        self.assertEqual(args.max_turns, 40)

    def test_task_and_task_file_mutually_exclusive(self) -> None:
        exit_code, out = _run_main(["reason", "--task", "hi", "--task-file", "/tmp/x.txt"])
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["error"]["class"], "usage-error")
        self.assertEqual(envelope["mode"], "reason")

    def test_usage_error_stdout_is_exactly_one_json_document(self) -> None:
        _, out = _run_main(["banana"])
        non_empty_lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty_lines), 1)
        json.loads(out)  # the entire capture parses as a single JSON document


class EntrypointSuccessTests(PreflightHarness):
    """The success and stored-write-failure paths each emit exactly one envelope."""

    def test_stdout_contains_exactly_one_json_document_on_both_paths(self) -> None:
        success_code, success_out = self.run_preflight()
        self.assertEqual(success_code, 0)
        non_empty = [line for line in success_out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1)
        success_envelope = json.loads(success_out)
        self.assertEqual(success_envelope["status"], "success")

        failure_code, failure_out = _run_main(["banana"])
        self.assertEqual(failure_code, 1)
        failure_non_empty = [line for line in failure_out.splitlines() if line.strip()]
        self.assertEqual(len(failure_non_empty), 1)
        json.loads(failure_out)

    def _flaky_emit_factory(self):
        """An emit_envelope stand-in that fails only the stored-copy write (path not None)."""
        real_emit = envelope_mod.emit_envelope

        def _flaky_emit(envelope, envelope_path):
            if envelope_path is not None:
                raise OSError("simulated stored-copy write failure")
            real_emit(envelope, None)

        return _flaky_emit

    def _drive_preflight_with_flaky_store(self, scenarios, source_grok_dir):
        """Drive main(["preflight"]) injecting per-home scenarios and a flaky stored-copy write."""
        from groklib.modes import preflight
        from groklib.authhome import create_private_home

        state = {"n": 0}

        def _patched_create(**kwargs):
            home = create_private_home(**kwargs)
            index = state["n"]
            scenario = scenarios[index] if index < len(scenarios) else scenarios[-1]
            state["n"] = index + 1
            (home.home_dir / "fake-grok-control.json").write_text(
                json.dumps({"scenario": scenario}), encoding="utf-8"
            )
            return home

        buffer = io.StringIO()
        with mock.patch.object(preflight, "_source_grok_dir", lambda: source_grok_dir):
            with mock.patch.object(preflight, "create_private_home", _patched_create):
                with mock.patch.object(grok_agent, "emit_envelope", self._flaky_emit_factory()):
                    with contextlib.redirect_stdout(buffer):
                        exit_code = grok_agent.main(["preflight"])
        return exit_code, buffer.getvalue()

    def test_stored_write_failure_reemits_original_envelope(self) -> None:
        # F2: a fully SUCCESSFUL run whose stored-copy write fails must re-emit the
        # SAME original success envelope (never a fabricated cli-failure); exactly
        # one envelope reaches stdout, with an appended stored-write warning.
        exit_code, out = self._drive_preflight_with_flaky_store(
            ("models-ok", "inspect-ok"), self.grok_home
        )
        non_empty = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1, "exactly one envelope must reach stdout")
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(envelope["status"], "success")
        self.assertNotIn("error", envelope)
        self.assertTrue(
            any("stored envelope copy could not be written" in warning for warning in envelope["warnings"]),
            envelope["warnings"],
        )

    def test_stored_write_failure_still_emits_exactly_one_envelope(self) -> None:
        # A FAILING run (auth-missing) whose stored-copy write fails re-emits the
        # SAME original failure envelope, preserving its real error class -- still
        # exactly one envelope on stdout.
        exit_code, out = self._drive_preflight_with_flaky_store(
            ("models-ok", "inspect-ok"), self.empty_grok_dir()
        )
        non_empty = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(non_empty), 1, "exactly one envelope must reach stdout")
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "auth-missing")


if __name__ == "__main__":
    unittest.main()
