# wrapper/scripts/tests/test_sandbox.py

import json
import os
import pathlib
import shutil
import tempfile
import unittest

from groklib import GrokWrapperError, sandbox
from groklib.authhome import PrivateHome
from groklib.sandbox import SandboxPolicy

from tests.probedplatform import ProbedPlatformMixin


class PolicyForModeTests(unittest.TestCase):
    """policy_for_mode: base-profile resolution, worktree requirement, write-confinement per D-SECRETREAD."""

    def setUp(self) -> None:
        self.scratch_dir = tempfile.mkdtemp(prefix="grok-cli-sandbox-policy-")
        self.addCleanup(shutil.rmtree, self.scratch_dir, True)
        self.private_tmp = pathlib.Path(self.scratch_dir) / "private-tmp"
        self.private_tmp.mkdir()
        self.worktree = pathlib.Path(self.scratch_dir) / "worktree"
        self.worktree.mkdir()

    def test_policy_returns_write_confinement_for_every_mode(self) -> None:
        # D-SECRETREAD (2026-07-14): policy_for_mode never raises
        # probe-required; every mode returns a write-confinement policy
        # immediately. review/reason resolve to the DISTINCT custom
        # grok-skills-<mode> profile (extending the read-only built-in) with no
        # writable roots; code/verify resolve to the grok-skills-<mode> profile
        # (extending the workspace built-in) with the worktree and private tmp
        # as the legitimate writable roots. The distinct name never shadows the
        # built-in (Grok dogfood-2 #6).
        cases = (
            ("review", None, "grok-skills-review", ()),
            ("reason", None, "grok-skills-reason", ()),
            (
                "code",
                self.worktree,
                "grok-skills-code",
                (str(self.worktree.resolve()), str(self.private_tmp.resolve())),
            ),
            (
                "verify",
                self.worktree,
                "grok-skills-verify",
                (str(self.worktree.resolve()), str(self.private_tmp.resolve())),
            ),
        )
        for mode, worktree, expected_profile, expected_writable_roots in cases:
            with self.subTest(mode=mode):
                policy = sandbox.policy_for_mode(mode, worktree=worktree, private_tmp=self.private_tmp)
                self.assertEqual(policy.mode, mode)
                self.assertEqual(policy.profile, expected_profile)
                # The custom profile name is never the built-in it extends.
                self.assertNotIn(policy.profile, ("read-only", "workspace"))
                self.assertEqual(policy.writable_roots, expected_writable_roots)

    def test_secret_read_denial_recorded_false_but_not_a_gate(self) -> None:
        # D-SECRETREAD: secret_read_denial_proven is recorded honestly as
        # False for every mode (the read gap is accepted, never proven
        # closed) but it is purely INFORMATIONAL -- it never prevents
        # policy_for_mode from succeeding for any mode.
        cases = (
            ("review", None),
            ("reason", None),
            ("code", self.worktree),
            ("verify", self.worktree),
        )
        for mode, worktree in cases:
            with self.subTest(mode=mode):
                policy = sandbox.policy_for_mode(mode, worktree=worktree, private_tmp=self.private_tmp)
                self.assertFalse(policy.secret_read_denial_proven)

    def test_policy_code_requires_worktree(self) -> None:
        # code is a write-capable mode; a missing worktree is a usage error
        # because there are no legitimate writable roots to resolve without
        # it. This precondition is unchanged by D-SECRETREAD.
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.policy_for_mode("code", worktree=None, private_tmp=self.private_tmp)
        self.assertEqual(caught.exception.error_class, "usage-error")

    def test_policy_unknown_mode_is_usage_error(self) -> None:
        # Extra coverage: an unknown mode fails closed as a usage error, never
        # a silent default.
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.policy_for_mode("deploy", worktree=None, private_tmp=self.private_tmp)
        self.assertEqual(caught.exception.error_class, "usage-error")

    def test_policy_rejects_relative_worktree_or_tmp(self) -> None:
        # Task 6 M1: worktree and private_tmp must be asserted absolute BEFORE
        # .resolve(), never silently resolved against the current working
        # directory. A relative private_tmp (checked for every mode) and a
        # relative worktree (checked for the write-capable modes) both fail
        # closed as usage errors.
        with self.assertRaises(GrokWrapperError) as caught_tmp:
            sandbox.policy_for_mode(
                "review", worktree=None, private_tmp=pathlib.Path("relative/private-tmp")
            )
        self.assertEqual(caught_tmp.exception.error_class, "usage-error")

        with self.assertRaises(GrokWrapperError) as caught_worktree:
            sandbox.policy_for_mode(
                "code", worktree=pathlib.Path("relative/worktree"), private_tmp=self.private_tmp
            )
        self.assertEqual(caught_worktree.exception.error_class, "usage-error")


class RenderSandboxTomlTests(unittest.TestCase):
    """render_sandbox_toml: extends the base write-confinement profile; deny_read_globs is best-effort only."""

    def _policy(self, mode: str, profile: str) -> SandboxPolicy:
        return SandboxPolicy(mode=mode, profile=profile, writable_roots=(), secret_read_denial_proven=False)

    def test_render_sandbox_toml_includes_best_effort_deny_globs_with_unenforced_comment(self) -> None:
        # D-SECRETREAD (2026-07-14): deny_read_globs is kept as a
        # best-effort, defense-in-depth artifact, but the rendered profile
        # MUST carry an explicit comment that it is NOT enforced on grok
        # 0.2.101 and must not be relied on. The write-confinement extends
        # stanza (the actually-enforced boundary) must remain intact
        # regardless.
        real_home = pathlib.Path("/Users/operator")
        policy = self._policy("review", "grok-skills-review")
        rendered = sandbox.render_sandbox_toml(policy, real_home=real_home)

        # Write-confinement profile stanza extends the mode's base built-in.
        self.assertIn("[profiles.grok-skills-review]", rendered)
        self.assertIn('extends = "read-only"', rendered)

        # Best-effort credential deny globs, defense-in-depth only.
        self.assertIn("deny_read_globs = [", rendered)
        for credential_dir in (".ssh", ".aws", ".grok", ".config"):
            self.assertIn('"/Users/operator/{}"'.format(credential_dir), rendered)
            self.assertIn('"/Users/operator/{}/**"'.format(credential_dir), rendered)
        # Keychain paths are macOS-only (platformsupport.credential_deny_dirs).
        from groklib import platformsupport

        if platformsupport.current_platform() == "macos":
            self.assertIn('"/Users/operator/Library/Keychains"', rendered)
            self.assertIn('"/Users/operator/Library/Keychains/**"', rendered)
            self.assertIn('"/Library/Keychains"', rendered)
            self.assertIn('"/Library/Keychains/**"', rendered)

        # D-SECRETREAD: explicit comment that these globs are NOT enforced
        # and must not be relied on.
        self.assertIn("D-SECRETREAD", rendered)
        self.assertIn("does NOT enforce read denial", rendered)
        self.assertIn("must NOT be relied on", rendered)

        # D-NET: no network restriction directive is added. (The word
        # "network" appears only in the explanatory comment about egress
        # being permitted; assert no restriction KEY is set on any
        # non-comment line.)
        self.assertNotIn("restrict_network", rendered)
        self.assertNotIn("deny_network", rendered)
        directive_lines = [
            line for line in rendered.splitlines() if line and not line.lstrip().startswith("#")
        ]
        for line in directive_lines:
            self.assertNotIn("network", line)

    def test_policy_for_mode_profile_never_self_extends_the_builtin(self) -> None:
        # Grok dogfood-2 #6: the profile policy_for_mode resolves for a REAL run,
        # rendered through render_sandbox_toml, must NOT produce a stanza that
        # names AND extends the same built-in (e.g. [profiles.read-only]
        # extends = "read-only"), which would shadow/redefine the built-in.
        private_tmp = pathlib.Path("/tmp").resolve()
        for mode, base_builtin in (("review", "read-only"), ("code", "workspace")):
            with self.subTest(mode=mode):
                worktree = pathlib.Path("/tmp/wt").resolve() if mode == "code" else None
                policy = sandbox.policy_for_mode(mode, worktree=worktree, private_tmp=private_tmp)
                self.assertNotEqual(policy.profile, base_builtin, "profile must not BE the built-in")
                rendered = sandbox.render_sandbox_toml(policy, real_home=pathlib.Path("/Users/operator"))
                self.assertIn("[profiles.{}]".format(policy.profile), rendered)
                self.assertIn('extends = "{}"'.format(base_builtin), rendered)
                # The self-extending shadow stanza must never appear.
                self.assertNotIn("[profiles.{}]".format(base_builtin), rendered)

    def test_render_sandbox_toml_workspace_base_for_code(self) -> None:
        policy = self._policy("code", "grok-skills-code")
        rendered = sandbox.render_sandbox_toml(policy, real_home=pathlib.Path("/Users/operator"))
        self.assertIn("[profiles.grok-skills-code]", rendered)
        self.assertIn('extends = "workspace"', rendered)

    def test_render_sandbox_toml_rejects_relative_home(self) -> None:
        policy = self._policy("review", "grok-skills-review")
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.render_sandbox_toml(policy, real_home=pathlib.Path("relative/home"))
        self.assertEqual(caught.exception.error_class, "usage-error")

    def test_render_sandbox_toml_rejects_unknown_mode(self) -> None:
        policy = SandboxPolicy(mode="deploy", profile="grok-skills-deploy", writable_roots=(), secret_read_denial_proven=False)
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.render_sandbox_toml(policy, real_home=pathlib.Path("/Users/operator"))
        self.assertEqual(caught.exception.error_class, "usage-error")


class VerifyEnforcementTests(ProbedPlatformMixin, unittest.TestCase):
    """verify_enforcement: parses <home>/.grok/sandbox-events.jsonl per Task 0 shapes, fails closed."""

    def setUp(self) -> None:
        super().setUp()
        self.scratch_dir = tempfile.mkdtemp(prefix="grok-cli-sandbox-verify-")
        self.addCleanup(shutil.rmtree, self.scratch_dir, True)
        self.home_dir = pathlib.Path(self.scratch_dir) / "grok-skills-home-fixture"
        self.grok_dir = self.home_dir / ".grok"
        self.grok_dir.mkdir(parents=True)
        self.home = PrivateHome(
            home_dir=self.home_dir,
            grok_dir=self.grok_dir,
            config_path=self.grok_dir / "config.toml",
        )
        self.worktree = pathlib.Path(self.scratch_dir) / "worktree"
        self.worktree.mkdir()

    def _policy(self, profile: str, writable_roots: "tuple[str, ...]" = ()) -> SandboxPolicy:
        return SandboxPolicy(
            mode="review",
            profile=profile,
            writable_roots=writable_roots,
            secret_read_denial_proven=False,
        )

    def _write_events(self, events: "list") -> None:
        events_path = self.grok_dir / "sandbox-events.jsonl"
        with open(str(events_path), "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def _profile_applied(self, profile: str) -> "dict":
        # Matches the Task 0 probe-report.md Step 5e ProfileApplied shape.
        return {
            "timestamp": "2026-07-14T19:30:00.000000Z",
            "event_type": "ProfileApplied",
            "profile": profile,
            "workspace": str(self.worktree),
            "platform": "macos/seatbelt",
            "enforced": True,
            "restrict_network": True,
            "read_write_paths": [str(self.grok_dir), "/private/tmp", str(self.worktree)],
            "read_only_paths": ["/usr", "/opt"],
        }

    def _fs_violation(self, profile: str, target: str, operation: str = "write") -> "dict":
        # Matches the Task 0 probe-report.md Step 5e FsViolation shape.
        return {
            "timestamp": "2026-07-14T19:30:01.000000Z",
            "event_type": "FsViolation",
            "profile": profile,
            "operation": operation,
            "target": target,
        }

    def test_verify_enforcement_happy_path_builds_c4_subobject(self) -> None:
        self._write_events([self._profile_applied("grok-skills-review")])
        result = sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(
            sorted(result.keys()),
            ["enforced", "evidence", "reportedProfile", "requestedProfile"],
        )
        self.assertEqual(result["requestedProfile"], "grok-skills-review")
        self.assertEqual(result["reportedProfile"], "grok-skills-review")
        self.assertTrue(result["enforced"])
        self.assertIsInstance(result["evidence"], str)
        self.assertIn("macos/seatbelt", result["evidence"])

    def test_verify_enforcement_missing_events_file_raises(self) -> None:
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_enforcement_missing_profile_applied_raises(self) -> None:
        # Only an FsViolation, no ProfileApplied: no proof the profile was
        # applied at all, so fail closed.
        self._write_events([self._fs_violation("grok-skills-review", "/Users/operator/.ssh/id_rsa", "read")])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_enforcement_profile_mismatch_raises(self) -> None:
        self._write_events([self._profile_applied("some-other-profile")])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_enforcement_enforced_false_raises(self) -> None:
        applied = self._profile_applied("grok-skills-review")
        applied["enforced"] = False
        self._write_events([applied])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_enforcement_records_fs_violations_as_evidence(self) -> None:
        # An FsViolation whose target is OUTSIDE the writable roots is the
        # sandbox correctly denying an escape: healthy evidence, not a
        # failure.
        blocked_target = "/Users/operator/grok-skills-escape/escape.txt"
        self._write_events(
            [
                self._profile_applied("grok-skills-code"),
                self._fs_violation("grok-skills-code", blocked_target, "write"),
            ]
        )
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        result = sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(result["reportedProfile"], "grok-skills-code")
        self.assertIn(blocked_target, result["evidence"])

    def test_verify_enforcement_fs_violation_inside_writable_root_raises(self) -> None:
        # Extra coverage: an FsViolation blocking a write INSIDE a legitimate
        # writable root means the applied profile contradicts the intended
        # policy (a path the run is entitled to write was denied). Fail
        # closed rather than proceed with an inconsistent sandbox.
        inside_target = str(self.worktree / "src" / "edited.ts")
        self._write_events(
            [
                self._profile_applied("grok-skills-code"),
                self._fs_violation("grok-skills-code", inside_target, "write"),
            ]
        )
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_fs_violation_inside_writable_root_via_symlinked_spelling_raises(self) -> None:
        # PR968 codex fsviolation-resolve: policy.writable_roots are stored after
        # .resolve() (the /private/var spelling on macOS) while sandbox telemetry
        # may report the denial under the equivalent /var symlink spelling. The
        # guard must realpath BOTH sides so a BLOCKED write reported under the
        # symlinked spelling is still recognized as INSIDE the writable root and
        # fails closed, instead of being mistaken for harmless escape evidence.
        real_root = pathlib.Path(self.scratch_dir) / "real-worktree"
        real_root.mkdir()
        symlinked_root = pathlib.Path(self.scratch_dir) / "linked-worktree"
        os.symlink(str(real_root), str(symlinked_root))
        # The policy pins the RESOLVED root; the denial is reported under the
        # unresolved symlink spelling of the very same location.
        denied_target = str(symlinked_root / "src" / "edited.ts")
        self._write_events(
            [
                self._profile_applied("grok-skills-code"),
                self._fs_violation("grok-skills-code", denied_target, "write"),
            ]
        )
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(real_root.resolve()),),
            secret_read_denial_proven=False,
        )
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_rejects_write_allowlist_outside_expected_roots(self) -> None:
        # Task 6 M2: the applied read_write_paths must be a subset of the
        # policy's writable_roots plus the platform's mandatory session-temp
        # roots. A ProfileApplied that otherwise verifies (correct profile,
        # enforced, matching platform) but grants a write to an unexpected
        # location fails closed with sandbox-failure.
        applied = self._profile_applied("grok-skills-review")
        applied["read_write_paths"] = [str(self.grok_dir), "/private/tmp", "/opt/not-allowed"]
        self._write_events([applied])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_accepts_write_allowlist_within_worktree_and_session_temp(self) -> None:
        # Complement to the M2 rejection test: read_write_paths that fall
        # inside the policy's writable_roots (the worktree) and the platform's
        # session-temp roots (the private grok dir, /private/tmp) verify
        # cleanly. Guards against the subset check being too strict.
        applied = self._profile_applied("grok-skills-code")
        applied["read_write_paths"] = [str(self.grok_dir), "/private/tmp", str(self.worktree)]
        self._write_events([applied])
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        result = sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(result["reportedProfile"], "grok-skills-code")

    def test_verify_rejects_workspace_profile_missing_read_write_paths(self) -> None:
        # S1/SEC3: a write-capable (workspace) profile whose ProfileApplied omits
        # read_write_paths cannot have its write confinement verified, so it must
        # fail closed rather than silently skip the subset check and report
        # read_write_paths=0.
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        applied = self._profile_applied("grok-skills-code")
        del applied["read_write_paths"]
        self._write_events([applied])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_rejects_workspace_profile_non_list_read_write_paths(self) -> None:
        # S1/SEC3: a non-list read_write_paths on a write-capable profile is
        # equally unverifiable and fails closed.
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        applied = self._profile_applied("grok-skills-code")
        applied["read_write_paths"] = "not-a-list"
        self._write_events([applied])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_rejects_write_capable_profile_empty_read_write_paths(self) -> None:
        # Grok r5 #3: an EMPTY read_write_paths on a write-capable profile passes the
        # subset check vacuously yet proves NOTHING about write confinement (it could
        # report enforced=true while the real profile still allows cwd writes). It
        # must fail closed because the expected writable roots are not granted.
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        applied = self._profile_applied("grok-skills-code")
        applied["read_write_paths"] = []
        self._write_events([applied])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_verify_rejects_write_capable_profile_missing_expected_writable_root(self) -> None:
        # Grok r5 #3: a write-capable profile whose grants stay within the expected
        # roots but do NOT include the run's own worktree fails closed -- the expected
        # write grant must be PRESENT, not merely a non-empty list of session-temp
        # grants.
        policy = SandboxPolicy(
            mode="code",
            profile="grok-skills-code",
            writable_roots=(str(self.worktree),),
            secret_read_denial_proven=False,
        )
        applied = self._profile_applied("grok-skills-code")
        # A grant only to the session temp (grok dir), NOT the worktree writable root.
        applied["read_write_paths"] = [str(self.grok_dir), "/private/tmp"]
        self._write_events([applied])
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox.verify_enforcement(self.home, policy)
        self.assertEqual(caught.exception.error_class, "sandbox-failure")

    def test_read_only_profile_tolerates_absent_read_write_paths(self) -> None:
        # Complement: a read-only profile has no writable roots to confine, so an
        # absent read_write_paths list is acceptable and verifies cleanly.
        applied = self._profile_applied("grok-skills-review")
        del applied["read_write_paths"]
        self._write_events([applied])
        result = sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(result["reportedProfile"], "grok-skills-review")

    def test_verify_enforcement_skips_torn_last_line(self) -> None:
        # Extra coverage: a trailing partial JSON line (torn concurrent write)
        # is skipped, matching the C3 progress-stream reader discipline, and
        # a valid ProfileApplied earlier in the file still proves enforcement.
        events_path = self.grok_dir / "sandbox-events.jsonl"
        with open(str(events_path), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self._profile_applied("grok-skills-review")) + "\n")
            handle.write('{"event_type": "FsViol')  # torn, no newline
        result = sandbox.verify_enforcement(self.home, self._policy("grok-skills-review"))
        self.assertEqual(result["reportedProfile"], "grok-skills-review")


if __name__ == "__main__":
    unittest.main()
