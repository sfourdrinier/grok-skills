# wrapper/scripts/tests/test_cli_defaults.py
#
# TDD contracts for grok-4.6 default, effort vocabulary, --no-plan default vs
# opt-out, version non-pin, and the dual-language JSON SSOT. These tests drive
# shipped entry points (argparse, build_argv, peer argv, family assert,
# check_version, preflight requested-model) - they fail if a second literal
# copy of the default model or effort vocabulary reappears in production code.

from __future__ import annotations

import os
import pathlib
import re
import types
import unittest
from unittest import mock

import grok_agent
from groklib import GrokWrapperError, grokcli
from groklib import grokcli_version
from groklib.modes import _shared
from groklib.modes import peer_process
from groklib.modes import preflight

from tests.test_grokcli import GrokCliTestBase, _FAKE_BINARY


_PROD_GLOBS = (
    "groklib/**/*.py",
    "grok_agent.py",
)
_ALLOWED_DEFAULT_MODEL_FILES = frozenset(
    {
        "cli_defaults.py",
        "grok-cli-defaults.json",
    }
)


class CliDefaultsSsotTests(unittest.TestCase):
    def test_ssot_default_model_is_grok_4_6(self) -> None:
        from groklib.cli_defaults import DEFAULT_MODEL, cli_defaults_ssot_path, load_cli_defaults

        doc = load_cli_defaults()
        self.assertEqual(doc["defaultModel"], "grok-4.6")
        self.assertEqual(DEFAULT_MODEL, "grok-4.6")
        self.assertEqual(cli_defaults_ssot_path().name, "grok-cli-defaults.json")
        self.assertIn("references", cli_defaults_ssot_path().parts)

    def test_effort_vocabulary_is_low_medium_high_xhigh(self) -> None:
        from groklib.cli_defaults import REASONING_EFFORT_VALUES, parse_reasoning_effort

        self.assertEqual(REASONING_EFFORT_VALUES, ("low", "medium", "high", "xhigh"))
        for value in REASONING_EFFORT_VALUES:
            self.assertEqual(parse_reasoning_effort(value), value)
            self.assertEqual(parse_reasoning_effort(value.upper()), value)

    def test_invalid_or_blank_effort_fails_closed(self) -> None:
        from groklib.cli_defaults import parse_reasoning_effort

        for bad in ("", "   ", "turbo", "max", None, 3):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_reasoning_effort(bad)

    def test_no_second_default_model_or_effort_literal_in_production(self) -> None:
        from groklib.cli_defaults import DEFAULT_MODEL, REASONING_EFFORT_VALUES

        scripts = pathlib.Path(__file__).resolve().parents[1]
        hits_model = []
        hits_effort = []
        effort_re = re.compile(
            r"""["']low["']\s*,\s*["']medium["']\s*,\s*["']high["']\s*,\s*["']xhigh["']"""
        )
        for rel in _PROD_GLOBS:
            for path in scripts.glob(rel):
                if not path.is_file() or path.name.endswith(".pyc"):
                    continue
                text = path.read_text(encoding="utf-8")
                if DEFAULT_MODEL in text and path.name not in _ALLOWED_DEFAULT_MODEL_FILES:
                    hits_model.append(str(path.relative_to(scripts)))
                if effort_re.search(text) and path.name not in _ALLOWED_DEFAULT_MODEL_FILES:
                    hits_effort.append(str(path.relative_to(scripts)))
        self.assertEqual(
            hits_model,
            [],
            "default model id must live only in the JSON SSOT / cli_defaults loader: {}".format(
                hits_model
            ),
        )
        self.assertEqual(
            hits_effort,
            [],
            "effort vocabulary must not be retyped outside the SSOT: {}".format(hits_effort),
        )

    def test_no_hardcoded_grok_4_5_product_default_in_production(self) -> None:
        scripts = pathlib.Path(__file__).resolve().parents[1]
        stale = []
        needles = (
            'default="grok-4.5"',
            "default='grok-4.5'",
            'or "grok-4.5"',
            "or 'grok-4.5'",
            '_REQUESTED_MODEL = "grok-4.5"',
            "_REQUESTED_MODEL = 'grok-4.5'",
        )
        for rel in _PROD_GLOBS:
            for path in scripts.glob(rel):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                for needle in needles:
                    if needle in text:
                        stale.append("{}: {}".format(path.relative_to(scripts), needle))
        self.assertEqual(stale, [], "stale grok-4.5 product-default literals: {}".format(stale))


class WrapperParseAndArgvTests(GrokCliTestBase):
    def test_wrapper_parse_defaults_model_to_grok_4_6(self) -> None:
        from groklib.cli_defaults import DEFAULT_MODEL

        parser = grok_agent._build_parser()
        args = parser.parse_args(["review", "--target", "pkg", "--task", "x"])
        self.assertEqual(args.model, DEFAULT_MODEL)
        self.assertEqual(args.model, "grok-4.6")
        self.assertIsNone(getattr(args, "reasoning_effort", "missing"))
        self.assertTrue(args.no_plan)

    def test_wrapper_parse_explicit_grok_4_5_is_kept(self) -> None:
        parser = grok_agent._build_parser()
        args = parser.parse_args(
            ["review", "--target", "pkg", "--task", "x", "--model", "grok-4.5"]
        )
        self.assertEqual(args.model, "grok-4.5")

    def test_wrapper_parse_blank_model_equals_is_usage_error(self) -> None:
        parser = grok_agent._build_parser()
        with self.assertRaises(Exception) as caught:
            parser.parse_args(["review", "--target", "pkg", "--task", "x", "--model="])
        self.assertTrue(
            caught.exception.__class__.__name__ in ("_UsageError", "ArgumentError")
            or "model" in str(caught.exception).lower(),
            caught.exception,
        )

    def test_requested_model_from_args_blank_fails_closed(self) -> None:
        from groklib.cli_defaults import requested_model_from_args

        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(GrokWrapperError) as caught:
                    requested_model_from_args(types.SimpleNamespace(model=blank))
                self.assertEqual(caught.exception.error_class, "usage-error")

    def test_wrapper_parse_effort_last_wins_split_and_equals_and_alias(self) -> None:
        parser = grok_agent._build_parser()
        split = parser.parse_args(
            [
                "review",
                "--target",
                "pkg",
                "--task",
                "x",
                "--reasoning-effort",
                "low",
                "--effort",
                "xhigh",
            ]
        )
        self.assertEqual(split.reasoning_effort, "xhigh")
        eq = parser.parse_args(
            ["code", "--target", ".", "--base", "HEAD", "--task", "x", "--reasoning-effort=high"]
        )
        self.assertEqual(eq.reasoning_effort, "high")

    def test_wrapper_parse_invalid_effort_is_usage_error(self) -> None:
        parser = grok_agent._build_parser()
        with self.assertRaises(Exception) as caught:
            parser.parse_args(
                ["review", "--target", "pkg", "--task", "x", "--reasoning-effort", "turbo"]
            )
        self.assertTrue(
            caught.exception.__class__.__name__ in ("_UsageError", "ArgumentError")
            or "usage" in str(caught.exception).lower()
            or "effort" in str(caught.exception).lower(),
            caught.exception,
        )

    def test_wrapper_help_advertises_4_6_effort_and_plan_opt_out(self) -> None:
        parser = grok_agent._build_parser()
        help_text = parser.format_help()
        review = parser._subparsers._group_actions[0].choices["review"]
        review_help = review.format_help()
        combined = help_text + "\n" + review_help
        self.assertIn("grok-4.6", combined)
        self.assertNotIn("grok-composer-2.5-fast", combined)
        self.assertIn("--reasoning-effort", combined)
        self.assertIn("--effort", combined)
        self.assertTrue("--plan" in combined or "--no-plan" in combined)

    def test_build_argv_default_pins_no_plan_and_omits_effort(self) -> None:
        from groklib.cli_defaults import DEFAULT_MODEL

        argv = grokcli.build_argv(self._make_spec(model=DEFAULT_MODEL))
        self.assertEqual(argv[argv.index("--model") + 1], "grok-4.6")
        self.assertEqual(argv.count("--no-plan"), 1)
        self.assertNotIn("--reasoning-effort", argv)
        self.assertNotIn("--effort", argv)

    def test_build_argv_emits_one_reasoning_effort_and_allows_flag(self) -> None:
        self.assertIn("--reasoning-effort", grokcli.C6_BASELINE_FLAGS)
        argv = grokcli.build_argv(self._make_spec(reasoning_effort="xhigh"))
        self.assertEqual(argv.count("--reasoning-effort"), 1)
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "xhigh")
        self.assertNotIn("--effort", argv)

    def test_build_argv_plan_opt_out_omits_no_plan(self) -> None:
        argv = grokcli.build_argv(self._make_spec(no_plan=False))
        self.assertNotIn("--no-plan", argv)

    def test_build_argv_invalid_effort_fails_closed(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            grokcli.build_argv(self._make_spec(reasoning_effort="turbo"))
        self.assertEqual(caught.exception.error_class, "usage-error")


class FamilyAndPreflightTests(unittest.TestCase):
    def test_family_check_rejects_4_5_when_4_6_requested(self) -> None:
        from groklib.cli_defaults import is_same_model_family

        self.assertFalse(is_same_model_family("grok-4.5", "grok-4.6"))
        self.assertFalse(is_same_model_family("grok-4.5-build", "grok-4.6"))
        self.assertTrue(is_same_model_family("grok-4.6", "grok-4.6"))
        self.assertTrue(is_same_model_family("grok-4.6-build", "grok-4.6"))
        self.assertFalse(is_same_model_family("grok-4.5", "grok-4"))
        self.assertIs(_shared._is_same_model_family, is_same_model_family)

        class _Result:
            effective_model = "grok-4.5"

        with self.assertRaises(GrokWrapperError) as caught:
            _shared._assert_effective_model(_Result(), "grok-4.6")
        self.assertEqual(caught.exception.error_class, "model-unavailable")

    def test_preflight_requires_default_model_not_grok_4_5(self) -> None:
        from groklib.cli_defaults import DEFAULT_MODEL

        self.assertEqual(preflight._requested_model(), DEFAULT_MODEL)
        self.assertEqual(preflight._requested_model(), "grok-4.6")
        self.assertNotEqual(preflight._requested_model(), "grok-4.5")

    def test_no_plan_default_is_read_from_ssot_not_hardcoded(self) -> None:
        from groklib import cli_defaults

        doc = cli_defaults.load_cli_defaults()
        self.assertIsInstance(doc["noPlanDefault"], bool)
        self.assertEqual(cli_defaults.NO_PLAN_DEFAULT, doc["noPlanDefault"])
        src = pathlib.Path(cli_defaults.__file__).read_text(encoding="utf-8")
        self.assertIn('NO_PLAN_DEFAULT = bool(doc["noPlanDefault"])', src)
        self.assertNotIn("NO_PLAN_DEFAULT = True", src)
        self.assertNotIn("noPlanDefault must be true", src)

    def test_ssot_load_failure_emits_classified_envelope(self) -> None:
        import json

        from groklib import cli_defaults
        from tests.test_entrypoint import _run_main

        missing = pathlib.Path("/nonexistent/grok-cli-defaults.json")
        cli_defaults.reset_cli_defaults_cache()
        try:
            with mock.patch.object(cli_defaults, "cli_defaults_ssot_path", return_value=missing):
                code, stdout = _run_main(["preflight"])
        finally:
            cli_defaults.reset_cli_defaults_cache()
        self.assertNotEqual(code, 0, stdout)
        self.assertTrue(stdout.strip(), "stdout must carry one envelope, not a traceback-only failure")
        env = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "cli-failure")
        self.assertRegex(env["error"]["message"], r"SSOT|cli-defaults", env["error"]["message"])

    def test_accepted_version_stamp_is_last_probed_not_last_seen(self) -> None:
        import json

        stamp_path = grokcli.ACCEPTED_VERSION_FILE
        doc = json.loads(stamp_path.read_text(encoding="utf-8"))
        version = str(doc.get("version") or "")
        self.assertIn("0.2.110", version, doc)
        self.assertNotIn(
            "1.0.3",
            version,
            "do not stamp an unprobed CLI build as validated; record it as last-seen only",
        )
        last_seen = "{} {}".format(doc.get("lastSeenWorking") or "", doc.get("note") or "")
        self.assertIn("1.0.3", last_seen, doc)

    def test_wrapper_skill_documents_effort_and_plan_on_every_run_mode(self) -> None:
        skill = pathlib.Path(__file__).resolve().parents[2] / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        for heading in ("### `reason`", "### `code`", "### `verify`"):
            start = text.find(heading)
            self.assertNotEqual(start, -1, "missing {} in {}".format(heading, skill))
            nxt = text.find("\n### ", start + len(heading))
            section = text[start : nxt if nxt != -1 else None]
            self.assertIn("--reasoning-effort", section, heading)
            self.assertIn("--plan", section, heading)


class PeerArgvDefaultsTests(unittest.TestCase):
    def test_peer_argv_defaults_model_and_no_plan(self) -> None:
        from groklib.cli_defaults import DEFAULT_MODEL

        argv = peer_process.build_acp_stdio_argv(
            binary=pathlib.Path("/usr/bin/true"),
            model=DEFAULT_MODEL,
            leader_socket=pathlib.Path("/tmp/s.sock"),
            policy=type("P", (), {"profile": "workspace"})(),
            tools=("read_file",),
            web_access=False,
        )
        self.assertEqual(argv[argv.index("--model") + 1], "grok-4.6")
        self.assertIn("--no-plan", argv)
        self.assertLess(argv.index("--no-plan"), argv.index("agent"))

    def test_peer_argv_forwards_effort_and_plan_opt_out(self) -> None:
        argv = peer_process.build_acp_stdio_argv(
            binary=pathlib.Path("/usr/bin/true"),
            model="grok-4.5",
            leader_socket=pathlib.Path("/tmp/s.sock"),
            policy=type("P", (), {"profile": "workspace"})(),
            tools=("read_file",),
            web_access=False,
            reasoning_effort="high",
            no_plan=False,
        )
        self.assertEqual(argv[argv.index("--model") + 1], "grok-4.5")
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")
        self.assertLess(argv.index("--reasoning-effort"), argv.index("agent"))
        self.assertNotIn("--no-plan", argv)


class VersionNonPinTests(GrokCliTestBase):
    def test_check_version_accepts_working_line_that_is_not_the_advisory_stamp(self) -> None:
        stamp = grokcli_version.last_validated_version()
        fake_line = "grok 9.9.9 (deadbeef) [fake]"
        self.assertNotEqual(fake_line, stamp)
        with mock.patch.dict(os.environ, {"FAKE_GROK_VERSION": fake_line}, clear=False):
            returned = grokcli.check_version(_FAKE_BINARY)
        self.assertEqual(returned, fake_line)

    def test_accepted_version_stamp_is_advisory_none(self) -> None:
        import json

        stamp_path = grokcli.ACCEPTED_VERSION_FILE
        doc = json.loads(stamp_path.read_text(encoding="utf-8"))
        self.assertEqual(doc.get("enforcement"), "none")


if __name__ == "__main__":
    unittest.main()
