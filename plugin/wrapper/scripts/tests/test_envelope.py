# wrapper/scripts/tests/test_envelope.py

import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest

from groklib.envelope import (
    ERROR_CLASSES,
    InvalidEnvelopeError,
    build_envelope,
    emit_envelope,
    exit_code_for,
    failure_envelope,
    validate_envelope,
)

_RUN_ID = "20260714T180924Z-abcdef"


class BuildEnvelopeTests(unittest.TestCase):
    """Covers build_envelope against the C4 schema: defaults, omission, rejection."""

    def test_build_minimal_success_envelope_has_all_required_fields(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")

        self.assertEqual(envelope["schemaVersion"], 1)
        self.assertEqual(envelope["runId"], _RUN_ID)
        self.assertEqual(envelope["mode"], "review")
        self.assertEqual(envelope["status"], "success")

        # All C4 fields except verifier/error are always present, never omitted.
        always_present = {
            "schemaVersion",
            "runId",
            "mode",
            "status",
            "requestedModel",
            "effectiveModel",
            "repository",
            "targetWorkspace",
            "effectiveWorkingDirectory",
            "baseRevision",
            "worktreePath",
            "worktreeBranch",
            "sandbox",
            "policy",
            "instructions",
            "grok",
            "usage",
            "response",
            "changedFiles",
            "diffSummary",
            "commands",
            "progressStreamPath",
            "warnings",
            "cleanup",
        }
        self.assertEqual(set(envelope.keys()), always_present)

        self.assertIsNone(envelope["requestedModel"])
        self.assertIsNone(envelope["effectiveModel"])
        self.assertIsNone(envelope["repository"])
        self.assertIsNone(envelope["targetWorkspace"])
        self.assertIsNone(envelope["effectiveWorkingDirectory"])
        self.assertIsNone(envelope["baseRevision"])
        self.assertIsNone(envelope["worktreePath"])
        self.assertIsNone(envelope["worktreeBranch"])
        self.assertEqual(
            envelope["sandbox"],
            {"requestedProfile": None, "reportedProfile": None, "enforced": None, "evidence": None},
        )
        self.assertEqual(
            envelope["policy"],
            {"tools": [], "permissionMode": None, "subagents": False, "webAccess": False, "memory": False},
        )
        self.assertEqual(envelope["instructions"], [])
        self.assertEqual(
            envelope["grok"],
            {"sessionId": None, "requestId": None, "stopReason": None, "modelUsage": None},
        )
        self.assertEqual(envelope["usage"], {"turns": None, "raw": None})
        self.assertIsNone(envelope["response"])
        self.assertEqual(envelope["changedFiles"], [])
        self.assertIsNone(envelope["diffSummary"])
        self.assertEqual(envelope["commands"], [])
        self.assertIsNone(envelope["progressStreamPath"])
        self.assertEqual(envelope["warnings"], [])
        self.assertEqual(envelope["cleanup"], {"status": "not-applicable", "detail": None})

        self.assertEqual(validate_envelope(envelope), [])

    def test_build_envelope_defaults_are_not_shared_mutable_state(self) -> None:
        first = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        first["changedFiles"].append("mutated.py")
        first["policy"]["tools"].append("mutated-tool")

        second = build_envelope(run_id=_RUN_ID, mode="review", status="success")

        self.assertEqual(second["changedFiles"], [])
        self.assertEqual(second["policy"]["tools"], [])

    def test_unknown_top_level_key_rejected(self) -> None:
        with self.assertRaises(InvalidEnvelopeError):
            build_envelope(run_id=_RUN_ID, mode="review", status="success", bogusField="nope")

    def test_core_field_name_collision_rejected(self) -> None:
        # "runId" (the JSON key) must not be smuggled in via **fields under
        # its own name; run_id is the only legal way to set it.
        with self.assertRaises(InvalidEnvelopeError):
            build_envelope(run_id=_RUN_ID, mode="review", status="success", runId="other-run-id")

    def test_error_class_must_be_registered(self) -> None:
        for error_class in ERROR_CLASSES:
            envelope = failure_envelope(
                run_id=_RUN_ID, mode="review", error_class=error_class, message="boom"
            )
            self.assertEqual(envelope["error"]["class"], error_class)

        with self.assertRaises(InvalidEnvelopeError):
            failure_envelope(run_id=_RUN_ID, mode="review", error_class="banana", message="boom")

    def test_verifier_and_error_omitted_not_null_when_absent(self) -> None:
        success_envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        self.assertNotIn("verifier", success_envelope)
        self.assertNotIn("error", success_envelope)

        failure = failure_envelope(run_id=_RUN_ID, mode="review", error_class="timeout", message="took too long")
        self.assertNotIn("verifier", failure)
        self.assertIn("error", failure)

        verified = build_envelope(
            run_id=_RUN_ID,
            mode="verify",
            status="success",
            verifier={"identity": "grok-verifier", "verdict": "pass"},
        )
        self.assertEqual(verified["verifier"], {"identity": "grok-verifier", "verdict": "pass"})
        self.assertNotIn("error", verified)

    def test_failure_envelope_sets_status_failure_and_error_detail(self) -> None:
        envelope = failure_envelope(
            run_id=_RUN_ID,
            mode="code",
            error_class="cli-failure",
            message="grok exited nonzero",
            detail={"exitStatus": 2},
        )

        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"], {"class": "cli-failure", "message": "grok exited nonzero", "detail": {"exitStatus": 2}})
        self.assertEqual(exit_code_for(envelope), 1)

    def test_failure_envelope_default_detail_is_null(self) -> None:
        envelope = failure_envelope(run_id=_RUN_ID, mode="preflight", error_class="auth-missing", message="no auth")

        self.assertIsNone(envelope["error"]["detail"])

    def test_error_classes_is_exact_c4_tuple(self) -> None:
        self.assertEqual(
            ERROR_CLASSES,
            (
                "auth-missing",
                "version-mismatch",
                "model-unavailable",
                "invalid-target",
                "rules-parity-failure",
                "worktree-failure",
                "sandbox-failure",
                "wrong-working-directory",
                "tool-unavailable",
                "verifier-unavailable",
                "output-missing",
                "output-malformed",
                "schema-mismatch",
                "timeout",
                "turn-exhaustion",
                "cancelled",
                "cli-failure",
                "unexpected-edits",
                "validation-failure",
                "cleanup-failure",
                "state-ownership-violation",
                "leader-socket-failure",
                "usage-error",
                "probe-required",
                "finalization-timeout",
                "finalization-worker-missing-result",
                "finalization-worker-unkillable",
                "isolation-unavailable",
                "implementation-contract-invalid",
                "write-scope-violation",
                "unexpected-commit",
                "artifact-generation-failure",
                "artifact-integrity-failure",
                "handoff-unavailable",
                "terminal-envelope-incomplete",
                "acp-failure",
                "protected-path-write",
                "dirty-path-conflict",
            ),
        )

class ExitCodeForTests(unittest.TestCase):
    def test_exit_semantics_helper(self) -> None:
        success = build_envelope(run_id=_RUN_ID, mode="status", status="success")
        running = build_envelope(run_id=_RUN_ID, mode="status", status="running")
        failure = failure_envelope(run_id=_RUN_ID, mode="status", error_class="usage-error", message="bad args")

        self.assertEqual(exit_code_for(success), 0)
        self.assertEqual(exit_code_for(running), 0)
        self.assertEqual(exit_code_for(failure), 1)

    def test_incomplete_stop_exits_nonzero_even_when_status_success(self) -> None:
        # Cancelled-with-findings keeps status success so response is not wiped,
        # but incompleteStop must not look like a trustworthy completion (exit 1).
        incomplete = build_envelope(
            run_id=_RUN_ID,
            mode="code",
            status="success",
            incompleteStop=True,
            warnings=["findings kept (run may be incomplete)"],
        )
        self.assertTrue(incomplete.get("incompleteStop"))
        self.assertEqual(validate_envelope(incomplete), [])
        self.assertEqual(exit_code_for(incomplete), 1)

class ValidateEnvelopeTests(unittest.TestCase):
    """Covers validate_envelope's hand-rolled structural checking against FIELD_SPECS."""

    def test_valid_envelope_has_no_violations(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        self.assertEqual(validate_envelope(envelope), [])

    def test_validate_envelope_flags_missing_required_and_wrong_types(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        del envelope["runId"]
        envelope["warnings"] = "not-a-list"

        violations = validate_envelope(envelope)

        self.assertTrue(any("runId" in violation for violation in violations))
        self.assertTrue(any("warnings" in violation for violation in violations))

    def test_validate_envelope_flags_wrong_schema_version(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope["schemaVersion"] = 2

        violations = validate_envelope(envelope)

        self.assertTrue(any("schemaVersion" in violation for violation in violations))

    def test_validate_envelope_flags_bad_mode_and_status(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope["mode"] = "not-a-mode"
        envelope["status"] = "not-a-status"

        violations = validate_envelope(envelope)

        self.assertTrue(any("mode" in violation for violation in violations))
        self.assertTrue(any("status" in violation for violation in violations))

    def test_validate_envelope_flags_unknown_top_level_field(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope["bogusField"] = "nope"

        violations = validate_envelope(envelope)

        self.assertTrue(any("bogusField" in violation for violation in violations))

    def test_validate_envelope_flags_bad_nested_sandbox_field(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope["sandbox"]["enforced"] = "yes"

        violations = validate_envelope(envelope)

        self.assertTrue(any("sandbox" in violation and "enforced" in violation for violation in violations))

    def test_validate_envelope_flags_bad_instructions_item(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope["instructions"] = [{"path": "CLAUDE.md", "bytes": "not-an-int", "sha256": "abc"}]

        violations = validate_envelope(envelope)

        self.assertTrue(any("instructions" in violation and "bytes" in violation for violation in violations))

    def test_validate_envelope_accepts_verifier_and_error_when_present(self) -> None:
        envelope = build_envelope(
            run_id=_RUN_ID,
            mode="verify",
            status="success",
            verifier={"identity": "grok-verifier", "verdict": "pass"},
        )
        self.assertEqual(validate_envelope(envelope), [])

        failure = failure_envelope(run_id=_RUN_ID, mode="verify", error_class="verifier-unavailable", message="x")
        self.assertEqual(validate_envelope(failure), [])

    def test_validate_envelope_flags_bad_error_class(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope["error"] = {"class": "banana", "message": "boom", "detail": None}

        violations = validate_envelope(envelope)

        self.assertTrue(any("error" in violation for violation in violations))

    def test_validate_envelope_rejects_non_dict(self) -> None:
        self.assertNotEqual(validate_envelope("not-a-dict"), [])
        self.assertNotEqual(validate_envelope(None), [])

class EmitEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-envelope-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)

    def test_emit_envelope_prints_single_json_document(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            emit_envelope(envelope, None)

        output = captured.getvalue()
        self.assertEqual(output.count("\n"), 1)
        self.assertTrue(output.endswith("\n"))
        parsed = json.loads(output)
        self.assertEqual(parsed, envelope)

    def test_emit_envelope_with_none_path_stores_no_copy(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            emit_envelope(envelope, None)
        # Nothing to assert about a stored copy: there is no path to check.
        # The absence of a raised exception is the behavior under test.

    def test_emit_envelope_stores_copy_with_0600(self) -> None:
        envelope = build_envelope(run_id=_RUN_ID, mode="review", status="success")
        envelope_path = pathlib.Path(self.tmp_root) / "envelope.json"

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            emit_envelope(envelope, envelope_path)

        self.assertTrue(envelope_path.is_file())
        mode = stat.S_IMODE(os.stat(envelope_path).st_mode)
        self.assertEqual(mode, 0o600)

        with open(envelope_path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored, envelope)

    def test_do_not_store_is_valid_optional_field(self) -> None:
        env = failure_envelope(run_id=_RUN_ID, mode="review", error_class="cancelled", message="x")
        self.assertEqual(validate_envelope(env), [])
        env["doNotStore"] = True
        self.assertEqual(validate_envelope(env), [])

    def test_emit_strips_do_not_store_from_stored_copy(self) -> None:
        env = failure_envelope(run_id=_RUN_ID, mode="review", error_class="cancelled", message="x")
        env["doNotStore"] = True
        envelope_path = pathlib.Path(self.tmp_root) / "ephemeral.json"
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            emit_envelope(env, envelope_path)
        stdout_doc = json.loads(captured.getvalue())
        self.assertTrue(stdout_doc.get("doNotStore"))
        stored = json.loads(envelope_path.read_text(encoding="utf-8"))
        self.assertNotIn("doNotStore", stored)

    def test_emit_envelope_stored_copy_matches_stdout_document(self) -> None:
        envelope = failure_envelope(run_id=_RUN_ID, mode="code", error_class="timeout", message="took too long")
        envelope_path = pathlib.Path(self.tmp_root) / "envelope.json"

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            emit_envelope(envelope, envelope_path)

        with open(envelope_path, "r", encoding="utf-8") as handle:
            stored_text = handle.read()

        self.assertEqual(json.loads(stored_text), json.loads(captured.getvalue()))
