# wrapper/scripts/tests/test_mode_status.py

import contextlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import grok_agent
from groklib import runstate
from groklib.envelope import build_envelope
from groklib.progress import ProgressWriter


def _run_status(run_id):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = grok_agent.main(["status", "--run-id", run_id])
    return exit_code, buffer.getvalue()


class StatusModeTests(unittest.TestCase):
    """status reads the stored run and is strictly read-only over the run directory."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-status-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _seed_run(self, *, stored_envelope=None, with_progress=True):
        paths = runstate.create_run("review")
        record = {
            "schemaVersion": 1,
            "runId": paths.run_id,
            "mode": "review",
            "createdAtUtc": "2026-07-14T00:00:00+00:00",
            "status": "success",
            "requestedModel": "grok-4.5",
            "repository": None,
            "targetWorkspace": None,
            "worktreePath": None,
            "worktreeBranch": None,
            "baseRevision": None,
            "progressStreamPath": str(paths.progress_path),
            "envelopePath": str(paths.envelope_path),
        }
        runstate.write_run_record(paths, record)

        if with_progress:
            writer = ProgressWriter(paths.run_id, paths.progress_path)
            writer.emit("start", "run created", data={"mode": "review"})
            writer.emit("done", "run complete")

        if stored_envelope is None:
            stored_envelope = build_envelope(
                run_id=paths.run_id, mode="review", status="success", response={"answer": "PONG"}
            )
        paths.envelope_path.write_text(json.dumps(stored_envelope), encoding="utf-8")
        return paths

    def test_status_returns_stored_envelope_and_events(self) -> None:
        paths = self._seed_run()
        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["mode"], "status")
        response = envelope["response"]
        self.assertEqual(response["storedEnvelope"]["runId"], paths.run_id)
        self.assertEqual(response["storedEnvelope"]["response"], {"answer": "PONG"})
        self.assertEqual(len(response["events"]), 2)
        self.assertEqual(response["events"][0]["phase"], "start")
        self.assertIsInstance(response["eventWarnings"], list)
        self.assertTrue(response["target"]["hasStoredEnvelope"])
        self.assertEqual(response["target"]["recordStatus"], "success")

    def test_status_in_progress_is_running_not_success(self) -> None:
        # Live run: no envelope.json yet, run.json status=running, process lease alive.
        paths = runstate.create_run("review")
        record = {
            "schemaVersion": 1,
            "runId": paths.run_id,
            "mode": "review",
            "createdAtUtc": "2026-07-14T00:00:00+00:00",
            "status": "running",
            "requestedModel": "grok-4.5",
            "repository": "/tmp/repo",
            "targetWorkspace": None,
            "worktreePath": None,
            "worktreeBranch": None,
            "baseRevision": None,
            "progressStreamPath": str(paths.progress_path),
            "envelopePath": str(paths.envelope_path),
        }
        runstate.write_run_record(paths, record)
        writer = ProgressWriter(paths.run_id, paths.progress_path)
        writer.emit("start", "review run created", data={"mode": "review"})
        writer.emit("grok", "spawning grok cli", data={"model": "grok-4.5"})
        # Keep this test process as the "alive" owner for owner.pid
        runstate.write_home_liveness_marker(paths.run_dir, os.getpid())

        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "running")
        self.assertEqual(envelope["mode"], "status")
        response = envelope["response"]
        self.assertIsNone(response["storedEnvelope"])
        self.assertFalse(any("stored envelope not found" in str(w) for w in envelope.get("warnings") or []))
        self.assertEqual(response["target"]["recordStatus"], "running")
        self.assertEqual(response["target"]["process"], "alive")
        self.assertEqual(response["target"]["eventCount"], 2)
        self.assertIsNotNone(response["target"]["lastEvent"])
        self.assertIn("still in progress", response.get("summary", "").lower())
        self.assertEqual(len(response["events"]), 2)

    def test_status_finished_without_envelope_warns(self) -> None:
        # Dead process + no envelope: interrupted run, not "still running"
        paths = runstate.create_run("review")
        record = {
            "schemaVersion": 1,
            "runId": paths.run_id,
            "mode": "review",
            "createdAtUtc": "2026-07-14T00:00:00+00:00",
            "status": "running",
            "requestedModel": "grok-4.5",
            "repository": None,
            "targetWorkspace": None,
            "worktreePath": None,
            "worktreeBranch": None,
            "baseRevision": None,
            "progressStreamPath": str(paths.progress_path),
            "envelopePath": str(paths.envelope_path),
        }
        runstate.write_run_record(paths, record)
        # Stale pid that is not alive
        runstate.write_home_liveness_marker(paths.run_dir, 999999999)

        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)
        # Dead owner + no envelope → derived interrupted, top-level failure (read-only)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["response"]["target"]["lifecycle"], "interrupted")
        self.assertEqual(envelope["response"]["target"]["lifecycleSource"], "derived")
        self.assertTrue(
            any("no stored envelope" in str(w) for w in envelope.get("warnings") or []),
            envelope.get("warnings"),
        )

    def test_status_unknown_run_id_fails_with_invalid_target(self) -> None:
        missing_id = "20200101T000000Z-abcdef"
        exit_code, out = _run_status(missing_id)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["error"]["class"], "invalid-target")
        self.assertIn(missing_id, envelope["error"]["message"])

    def test_status_missing_owner_marker_fails_with_invalid_target(self) -> None:
        # PR968 codex status-ownership: a run dir with a valid run.json but NO owner.json
        # marker is not a genuine wrapper-owned target. status must fail closed as
        # invalid-target (companion renders nothing), never return a success envelope.
        paths = self._seed_run()
        (paths.run_dir / "owner.json").unlink()
        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "invalid-target")
        self.assertEqual(envelope["status"], "failure")

    def test_status_mismatched_owner_marker_fails_with_invalid_target(self) -> None:
        # A valid, correctly-shaped owner marker that names a DIFFERENT run id must not
        # authorize status for the requested run -- it fails closed as invalid-target.
        paths = self._seed_run()
        other_run_id = runstate.new_run_id()
        self.assertNotEqual(other_run_id, paths.run_id)
        runstate.write_owner_marker_file(paths.run_dir / "owner.json", other_run_id)
        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(envelope["error"]["class"], "invalid-target")
        self.assertEqual(envelope["error"]["detail"]["markerRunId"], other_run_id)
        self.assertEqual(envelope["error"]["detail"]["requestedRunId"], paths.run_id)

    def test_status_shell_injection_run_id_rejected_as_literal(self) -> None:
        # PR968 codex #1: the run id reaches the wrapper as a single literal argv
        # element. A value carrying command-substitution / shell metacharacters is
        # not a valid run id, so it is rejected as invalid-target -- never split,
        # evaluated, or partially matched. The value is echoed back verbatim in the
        # detail, proving it was treated as opaque data, not shell syntax.
        for hostile in ("$(rm -rf ~)", "abc; touch pwned", "a && b", "`id`"):
            with self.subTest(hostile=hostile):
                exit_code, out = _run_status(hostile)
                envelope = json.loads(out)
                self.assertEqual(exit_code, 1)
                self.assertEqual(envelope["error"]["class"], "invalid-target")
                self.assertEqual(envelope["error"]["detail"]["runId"], hostile)

    def test_status_malformed_stored_envelope_classified(self) -> None:
        # A JSON object that is NOT a valid C4 envelope (missing required keys).
        paths = self._seed_run(stored_envelope={"schemaVersion": 1, "not": "an envelope"})
        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["error"]["class"], "output-malformed")
        # The malformed stored document is NOT re-emitted verbatim.
        self.assertNotIn("not", envelope.get("response") or {})

    def test_status_redacts_secret_shaped_streamed_thought(self) -> None:
        # F-STATUS-SECRET (D-STREAM regression): a streamed thought token whose
        # text carries a "bearer "/JWT/sk- shape must NOT permanently fail
        # readback. status embeds the events into the stdout envelope after
        # redacting the secret-shaped substrings, so build_envelope's
        # assert_no_secret_material passes and status returns success.
        paths = runstate.create_run("review")
        record = {
            "schemaVersion": 1,
            "runId": paths.run_id,
            "mode": "review",
            "createdAtUtc": "2026-07-14T00:00:00+00:00",
            "status": "success",
            "requestedModel": "grok-4.5",
            "repository": None,
            "targetWorkspace": None,
            "worktreePath": None,
            "worktreeBranch": None,
            "baseRevision": None,
            "progressStreamPath": str(paths.progress_path),
            "envelopePath": str(paths.envelope_path),
        }
        runstate.write_run_record(paths, record)

        secret_thought = (
            "the handler reads Authorization: bearer "
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w "
            "and also an api key sk-abcdef0123456789ABCDEFGHIJKLMNOP"
        )
        writer = ProgressWriter(paths.run_id, paths.progress_path)
        writer.emit("start", "run created", data={"mode": "review"})
        writer.emit(
            "grok",
            "grok streamed thought tokens",
            data={"event": "thought", "chars": len(secret_thought), "text": secret_thought},
        )
        stored_envelope = build_envelope(
            run_id=paths.run_id, mode="review", status="success", response={"answer": "PONG"}
        )
        paths.envelope_path.write_text(json.dumps(stored_envelope), encoding="utf-8")

        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)

        # Readback SUCCEEDS (no validation-failure), and the embedded event text
        # is redacted, not raw.
        self.assertEqual(exit_code, 0)
        self.assertEqual(envelope["status"], "success")
        events = envelope["response"]["events"]
        thought_event = next(
            event for event in events if isinstance(event.get("data"), dict) and event["data"].get("event") == "thought"
        )
        embedded_text = thought_event["data"]["text"]
        self.assertNotIn("bearer ", embedded_text)
        self.assertNotIn("sk-abcdef0123456789ABCDEFGHIJKLMNOP", embedded_text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", embedded_text)
        self.assertIn("[redacted-", embedded_text)

        # On-disk progress is redacted at write time (same patterns + denylist).
        raw_progress = paths.progress_path.read_text(encoding="utf-8")
        self.assertNotIn("bearer ", raw_progress)
        self.assertNotIn("sk-abcdef0123456789ABCDEFGHIJKLMNOP", raw_progress)
        self.assertIn("[redacted-", raw_progress)

    def test_status_redacts_secret_split_across_two_events(self) -> None:
        # Grok dogfood-3 #6: the coalescer batches raw tokens into ~480-char event
        # chunks, so a credential can be SPLIT across two consecutive events. No
        # single event matches a secret pattern, but the concatenation IS the
        # secret. status must redact on the JOINED text so neither half leaks.
        paths = runstate.create_run("review")
        record = {
            "schemaVersion": 1,
            "runId": paths.run_id,
            "mode": "review",
            "createdAtUtc": "2026-07-14T00:00:00+00:00",
            "status": "success",
            "requestedModel": "grok-4.5",
            "repository": None,
            "targetWorkspace": None,
            "worktreePath": None,
            "worktreeBranch": None,
            "baseRevision": None,
            "progressStreamPath": str(paths.progress_path),
            "envelopePath": str(paths.envelope_path),
        }
        runstate.write_run_record(paths, record)

        first_half = "the handler logs bearer eyJab"
        second_half = "cdef0123456789ABCDEFghijKLM trailing"
        writer = ProgressWriter(paths.run_id, paths.progress_path)
        writer.emit("grok", "grok streamed text tokens", data={"event": "text", "chars": len(first_half), "text": first_half})
        writer.emit("grok", "grok streamed text tokens", data={"event": "text", "chars": len(second_half), "text": second_half})
        stored_envelope = build_envelope(
            run_id=paths.run_id, mode="review", status="success", response={"answer": "PONG"}
        )
        paths.envelope_path.write_text(json.dumps(stored_envelope), encoding="utf-8")

        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)

        self.assertEqual(exit_code, 0)
        # The tail fragment that only forms a secret when concatenated must not
        # survive anywhere in the emitted (stdout) envelope.
        self.assertNotIn("cdef0123456789ABCDEFghijKLM", out)
        # On-disk progress stays raw.
        raw_progress = paths.progress_path.read_text(encoding="utf-8")
        self.assertIn("cdef0123456789ABCDEFghijKLM", raw_progress)

    def test_status_writes_nothing(self) -> None:
        paths = self._seed_run()
        before_listing = sorted(os.listdir(paths.run_dir))
        before_mtime = os.stat(paths.run_dir).st_mtime_ns

        exit_code, _ = _run_status(paths.run_id)
        self.assertEqual(exit_code, 0)

        after_listing = sorted(os.listdir(paths.run_dir))
        after_mtime = os.stat(paths.run_dir).st_mtime_ns
        self.assertEqual(before_listing, after_listing)
        self.assertEqual(before_mtime, after_mtime)

    def test_status_run_dir_byte_identical(self) -> None:
        """status is read-only: recursive file contents unchanged after query."""
        paths = self._seed_run()
        before = {}
        for root, _dirs, files in os.walk(paths.run_dir):
            for name in files:
                fp = pathlib.Path(root) / name
                before[str(fp.relative_to(paths.run_dir))] = fp.read_bytes()
        exit_code, _out = _run_status(paths.run_id)
        self.assertEqual(exit_code, 0)
        after = {}
        for root, _dirs, files in os.walk(paths.run_dir):
            for name in files:
                fp = pathlib.Path(root) / name
                after[str(fp.relative_to(paths.run_dir))] = fp.read_bytes()
        self.assertEqual(before, after)

    def test_status_envelope_overrides_nonterminal_record(self) -> None:
        """Valid envelope + non-terminal lifecycle → effective lifecycle from envelope."""
        paths = runstate.create_run("review")
        env = build_envelope(
            run_id=paths.run_id, mode="review", status="success", response={"answer": "done"}
        )
        paths.envelope_path.write_text(json.dumps(env), encoding="utf-8")
        runstate.write_home_liveness_marker(paths.run_dir, 999999999)
        exit_code, out = _run_status(paths.run_id)
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["response"]["target"]["lifecycle"], "completed")
        self.assertEqual(envelope["response"]["target"]["lifecycleSource"], "envelope")


if __name__ == "__main__":
    unittest.main()
