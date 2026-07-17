# wrapper/scripts/tests/test_mode_handoff.py

import hashlib
import json
import pathlib
import unittest
from unittest import mock

from groklib import envelope as envelope_mod
from groklib import runstate
from groklib.modes import handoff as handoff_mode

from tests.modefixtures import ModeHarness


_RUN_ID = "20260716T120000Z-abcdef"


def _mini_patch(path: str = "pkg/a.ts") -> bytes:
    return (
        "diff --git a/{0} b/{0}\n"
        "index 1111111..2222222 100644\n"
        "--- a/{0}\n"
        "+++ b/{0}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n".format(path)
    ).encode("utf-8")


def _valid_manifest(run_id: str, patch_sha: str, ready: bool = True, patch_bytes: int = 0) -> dict:
    changed = (
        [{"path": "pkg/a.ts", "status": "modified", "oldPath": None}]
        if ready
        else []
    )
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "taskId": "t1",
        "baseRevision": "a" * 40,
        "resultTreeOid": "b" * 40,
        "createdAtUtc": "2026-07-16T12:00:00Z",
        "changedFiles": changed,
        "patch": {
            "format": "git-binary-full-index-v1",
            "relativePath": "artifacts/implementation.patch",
            "sha256": patch_sha,
            "bytes": patch_bytes if patch_bytes else (10 if ready else 0),
        },
        "validation": {
            "requiredCommandsPassed": True,
            "buildGatePassed": True,
            "allPassed": True,
            "sources": {},
        },
        "integration": {
            "ready": ready,
            "blockers": [] if ready else [{"kind": "no-changes", "message": "x"}],
        },
        "worktree": {"retained": True, "path": "/tmp/wt", "branch": "grok/code/" + run_id},
    }


class HandoffModeTests(ModeHarness):
    def _seed_code_run(self, *, ready_manifest: bool = True, write_envelope: bool = True, tamper_patch: bool = False):
        paths = runstate.create_run("code")
        run_id = paths.run_id
        rev = 0
        rec = runstate.set_lifecycle(paths, rev, "running")
        rev = int(rec["recordRevision"])
        rec = runstate.cas_update_run_record(
            paths,
            rev,
            {
                "repository": "/tmp/repo",
                "baseRevision": "a" * 40,
                "worktreePath": "/tmp/wt",
                "worktreeBranch": "grok/code/" + run_id,
                "status": "running",
            },
        )
        rev = int(rec["recordRevision"])
        rec = runstate.set_lifecycle(paths, rev, "finalizing")
        rev = int(rec["recordRevision"])
        artifacts = paths.run_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        patch_bytes = _mini_patch("pkg/a.ts")
        (artifacts / "implementation.patch").write_bytes(patch_bytes)
        sha = "0" * 64 if tamper_patch else hashlib.sha256(patch_bytes).hexdigest()
        manifest = _valid_manifest(
            run_id, sha, ready=ready_manifest, patch_bytes=len(patch_bytes)
        )
        (paths.run_dir / "implementation-handoff.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        if write_envelope:
            env = envelope_mod.build_envelope(
                run_id=run_id,
                mode="code",
                status="success",
                repository="/tmp/repo",
                baseRevision="a" * 40,
                changedFiles=["pkg/a.ts"] if ready_manifest else [],
                progressStreamPath=None,
                cleanup={"status": "retained", "detail": str(paths.run_dir)},
            )
            runstate.persist_terminal_envelope(paths, rev, env, lifecycle="completed")
        return run_id

    def test_handoff_ready_when_dual_condition_holds(self) -> None:
        run_id = self._seed_code_run(ready_manifest=True, write_envelope=True)
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["mode"], "handoff")
        self.assertTrue(env["response"]["integration"]["ready"])
        self.assertIn("patch", env["response"]["handoff"])

    def test_handoff_response_echoes_contract_summary(self) -> None:
        run_id = self._seed_code_run(ready_manifest=True, write_envelope=True)
        paths_dir = runstate.state_root() / "runs" / run_id
        manifest = json.loads(
            (paths_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        summary = {
            "taskId": "T-1",
            "objective": "ship handoff summary",
            "acceptanceCriteria": ["echoed on response", "display only"],
        }
        manifest["contractSummary"] = summary
        (paths_dir / "implementation-handoff.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["response"]["contractSummary"], summary)

    def test_handoff_response_redacts_bearer_in_contract_summary(self) -> None:
        # Phase 1 finding 4: echo path runs redact_secret_value_text over
        # objective/criteria. Split secret-shaped fixture per repo rule 8.
        run_id = self._seed_code_run(ready_manifest=True, write_envelope=True)
        paths_dir = runstate.state_root() / "runs" / run_id
        manifest = json.loads(
            (paths_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        bearer_body = "4f8a9b2c1d0e3f5a6b7c8d9e0f1a2b3c"
        bearer_secret = "Bearer " + bearer_body
        summary = {
            "taskId": "T-1",
            "objective": "objective clean",
            "acceptanceCriteria": ["must pass with " + bearer_secret],
        }
        manifest["contractSummary"] = summary
        (paths_dir / "implementation-handoff.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(env["status"], "success")
        echoed = env["response"]["contractSummary"]
        self.assertNotIn(bearer_body, json.dumps(echoed))
        self.assertIn("[redacted-bearer-token]", echoed["acceptanceCriteria"][0])
        self.assertEqual(echoed["objective"], "objective clean")

    def test_handoff_not_ready_without_envelope(self) -> None:
        run_id = self._seed_code_run(ready_manifest=True, write_envelope=False)
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "terminal-envelope-incomplete")

    def test_handoff_integrity_on_tampered_patch(self) -> None:
        run_id = self._seed_code_run(ready_manifest=True, write_envelope=True, tamper_patch=True)
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(env["error"]["class"], "artifact-integrity-failure")

    def test_handoff_unavailable_without_artifacts(self) -> None:
        paths = runstate.create_run("code")
        run_id = paths.run_id
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(env["error"]["class"], "handoff-unavailable")

    def test_handoff_not_for_review_mode(self) -> None:
        paths = runstate.create_run("review")
        run_id = paths.run_id
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(env["error"]["class"], "handoff-unavailable")

    def test_handoff_rejects_escaping_patch_relative_path(self) -> None:
        run_id = self._seed_code_run(ready_manifest=True, write_envelope=True)
        paths_dir = runstate.state_root() / "runs" / run_id
        manifest = json.loads((paths_dir / "implementation-handoff.json").read_text(encoding="utf-8"))
        manifest["patch"]["relativePath"] = "../escape.patch"
        (paths_dir / "implementation-handoff.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(env["error"]["class"], "artifact-integrity-failure")

    def test_handoff_invalid_utf8_manifest_is_integrity_failure(self) -> None:
        paths = runstate.create_run("code")
        run_id = paths.run_id
        rec = runstate.set_lifecycle(paths, 0, "running")
        rev = int(rec["recordRevision"])
        rec = runstate.cas_update_run_record(
            paths,
            rev,
            {
                "repository": "/tmp/repo",
                "baseRevision": "a" * 40,
                "status": "running",
            },
        )
        rev = int(rec["recordRevision"])
        runstate.set_lifecycle(paths, rev, "finalizing")
        # Invalid UTF-8 bytes (not a valid text file)
        (paths.run_dir / "implementation-handoff.json").write_bytes(b"\xff\xfe not utf8 {")
        code, out = self.drive(["handoff", "--run-id", run_id])
        env = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(env["error"]["class"], "artifact-integrity-failure")
        self.assertIn("utf-8", json.dumps(env["error"]).lower() + env["error"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
