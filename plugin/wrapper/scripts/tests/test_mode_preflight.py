# wrapper/scripts/tests/test_mode_preflight.py

import json
import os
import pathlib
import unittest
from unittest import mock

from groklib import platformsupport

from tests.preflightfixtures import PreflightHarness


class PreflightModeTests(PreflightHarness):
    """preflight verifies every readiness check in order and fails closed on the first failure."""

    def test_preflight_success_envelope_lists_checks(self) -> None:
        exit_code, out = self.run_preflight()
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["mode"], "preflight")
        self.assertIsNotNone(envelope["progressStreamPath"])

        response = envelope["response"]
        self.assertIn("platform", response)
        self.assertIn("platformProbed", response)

        checks_by_name = {check["name"]: check for check in response["checks"]}
        for expected in (
            "grokVersion",
            "authMaterial",
            "platformProbed",
            "privateHomeLifecycle",
            "login",
            "inspectHome",
            "sandboxPolicies",
            "secretReadDenial",
            "stateRootWritable",
            "staleHomeAudit",
        ):
            self.assertIn(expected, checks_by_name)

        # D-SECRETREAD informational advisory: recorded, value False, never a gate.
        self.assertEqual(checks_by_name["secretReadDenial"]["value"], False)

    def test_preflight_version_mismatch_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_GROK_VERSION": "grok 9.9.9 (deadbeef) [fake]"}):
            exit_code, out = self.run_preflight()
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "version-mismatch")

    def test_preflight_unexpected_error_terminalizes_real_run(self) -> None:
        # round3 F3-preflight: a non-classified exception escaping a check (here a
        # ValueError from the stale-home audit) must terminalize the REAL run --
        # run.json failure under the SAME run id the envelope carries -- not leave
        # run.json at "running" while a synthesized id is emitted.
        from groklib.modes import preflight

        def _boom(*args, **kwargs):
            raise ValueError("simulated non-classified preflight failure")

        with mock.patch.object(preflight, "_check_stale_audit", _boom):
            exit_code, out = self.run_preflight()
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "cli-failure")

        run_dir = (
            pathlib.Path(self.state_home) / "grok-skills" / "runs" / env["runId"]
        )
        record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["mode"], "preflight")

    def test_preflight_missing_auth_fails_closed(self) -> None:
        exit_code, out = self.run_preflight(source_grok_dir=self.empty_grok_dir())
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "auth-missing")

    def test_preflight_unprobed_platform_is_not_ready(self) -> None:
        # SEC1: preflight is a live-run readiness diagnostic, so an unprobed
        # platform must surface not-ready (probe-required), never a false green.
        with mock.patch.object(platformsupport, "current_platform", lambda: "linux"):
            exit_code, out = self.run_preflight()
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "probe-required")

    def test_preflight_inspect_unclean_teardown_fails_closed(self) -> None:
        # S2: an unclean inspect-home teardown is a classified cleanup-failure,
        # consistent with the sibling login-home check -- never a passing check.
        from groklib.modes import preflight

        real_destroy = preflight.destroy_private_home
        calls = {"n": 0}

        def _flaky_destroy(home):
            calls["n"] += 1
            real_destroy(home)  # actually remove it so the private home never leaks
            # The first destroy is the login home (clean); the second is the
            # inspect home, which we simulate failing to tear down cleanly.
            if calls["n"] == 2:
                return {"status": "failed", "detail": "simulated unclean inspect teardown"}
            return {"status": "clean", "detail": None}

        with mock.patch.object(preflight, "destroy_private_home", _flaky_destroy):
            exit_code, out = self.run_preflight()
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "cleanup-failure")
        # F2 preflight-cleanup-field-on-classified-failure: the top-level cleanup
        # field must SURFACE the failed probe-home teardown (consistently with the
        # runners), not silently default to not-applicable and hide a possibly
        # leaked auth copy.
        self.assertEqual(envelope["cleanup"]["status"], "failed")
        self.assertEqual(envelope["cleanup"]["detail"], "simulated unclean inspect teardown")

    def test_preflight_success_cleanup_field_is_not_applicable(self) -> None:
        # A fully-passing preflight owns no persistent home; its probe homes are
        # torn down clean, so the top-level cleanup field stays not-applicable.
        exit_code, out = self.run_preflight()
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["cleanup"]["status"], "not-applicable")


if __name__ == "__main__":
    unittest.main()
