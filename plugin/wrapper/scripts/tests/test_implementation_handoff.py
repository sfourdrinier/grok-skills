# wrapper/scripts/tests/test_implementation_handoff.py

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from groklib import GrokWrapperError
from groklib.command_evidence import build_command_evidence
from groklib.implementation_handoff import (
    capture_phase1_patch,
    compute_integration_ready,
    dual_condition_ready,
    list_changed_paths,
    primary_error_from_blockers,
    validate_implementation_handoff,
    write_manifest,
    HandoffBlocker,
    _STEP_ORDER,
)
from tests import gitfixtures


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class ValidateHandoffTests(unittest.TestCase):
    def _doc(self, **overrides):
        base = {
            "schemaVersion": 1,
            "runId": "20260716T020408Z-a82843",
            "taskId": "t1",
            "baseRevision": "a" * 40,
            "resultTreeOid": "b" * 40,
            "createdAtUtc": "2026-07-16T02:04:08Z",
            "changedFiles": [{"path": "a.ts", "status": "modified", "oldPath": None}],
            "patch": {
                "format": "git-binary-full-index-v1",
                "relativePath": "artifacts/implementation.patch",
                "sha256": "c" * 64,
                "bytes": 10,
            },
            "validation": {
                "requiredCommandsPassed": True,
                "buildGatePassed": True,
                "allPassed": True,
                "sources": {},
            },
            "integration": {"ready": True, "blockers": []},
            "worktree": {"retained": True, "path": "/tmp/wt", "branch": "grok/code/x"},
        }
        base.update(overrides)
        return base

    def test_valid_doc(self) -> None:
        self.assertEqual(validate_implementation_handoff(self._doc()), [])

    def test_bad_patch_format(self) -> None:
        doc = self._doc()
        doc["patch"] = dict(doc["patch"], format="plain")
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("format" in e for e in errs))


class CommandEvidenceTests(unittest.TestCase):
    def test_tails_and_hashes(self) -> None:
        big = b"x" * 5000
        rec = build_command_evidence(
            argv=["echo", "hi"],
            cwd="/tmp",
            purpose="test",
            exit_status=0,
            stdout=big,
            stderr=b"secret-token-value-ABCDEF",
        )
        self.assertEqual(rec["stdoutSha256"], hashlib.sha256(big).hexdigest())
        self.assertTrue(rec["stdoutTail"]["truncated"])
        self.assertLessEqual(len(rec["stdoutTail"]["text"]), 4096)
        # redaction applied on stderr tail
        self.assertIn("stderrTail", rec)


class Phase1PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-handoff-")
        self.repo = gitfixtures.make_repo(self.tmp)
        (self.repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.artifacts = pathlib.Path(self.tmp) / "artifacts"
        self.artifacts.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_modify_delete_binary_untracked(self) -> None:
        (self.repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        (self.repo / "bin.dat").write_bytes(b"\x00\x01\x02\xff")
        (self.repo / "gone.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "gone.txt")
        _git(self.repo, "commit", "-q", "-m", "add gone")
        # rebase base to include gone, then delete
        self.base = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        (self.repo / "gone.txt").unlink()
        meta, path, tree, blockers, steps = capture_phase1_patch(
            worktree_path=self.repo,
            base_revision=self.base,
            artifacts_dir=self.artifacts,
            run_id="20260716T020408Z-a82843",
        )
        self.assertIsNotNone(meta)
        self.assertTrue(path and path.is_file())
        self.assertEqual(meta["format"], "git-binary-full-index-v1")
        self.assertEqual(meta["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertFalse(any(b.kind == "temp-index-retained" for b in blockers))
        self.assertIn("phase1-temp-index-cleaned", steps)
        # no leftover temp index
        leftovers = list(self.artifacts.glob("handoff.*.idx"))
        self.assertEqual(leftovers, [])
        # apply reconstructs
        apply_repo = pathlib.Path(self.tmp) / "apply"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.repo), str(apply_repo)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _git(apply_repo, "reset", "--hard", self.base)
        r = subprocess.run(
            ["git", "-C", str(apply_repo), "apply", "--check", "--binary", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_odd_paths_with_nul_safe_list(self) -> None:
        weird = self.repo / "has space.txt"
        weird.write_text("ok\n", encoding="utf-8")
        paths = list_changed_paths(self.repo, self.base)
        names = [p["path"] for p in paths]
        self.assertIn("has space.txt", names)

    def test_oversized_patch_fail_closed(self) -> None:
        (self.repo / "big.txt").write_bytes(b"Z" * 2000)
        with mock.patch.dict(os.environ, {"GROK_HANDOFF_PATCH_MAX_BYTES": str(100)}):
            # clamp min is 1 MiB in code — set below by mocking _patch_max_bytes
            with mock.patch(
                "groklib.implementation_handoff._patch_max_bytes", return_value=50
            ):
                meta, path, tree, blockers, steps = capture_phase1_patch(
                    worktree_path=self.repo,
                    base_revision=self.base,
                    artifacts_dir=self.artifacts,
                    run_id="20260716T020408Z-a82843",
                )
        self.assertIsNone(meta)
        self.assertTrue(any(b.kind == "artifact-too-large" for b in blockers))

    def test_temp_index_retained_blocker(self) -> None:
        (self.repo / "x.txt").write_text("x\n", encoding="utf-8")
        real_unlink = pathlib.Path.unlink

        def sticky_unlink(self, *args, **kwargs):
            if "handoff." in str(self) and str(self).endswith(".idx"):
                # pretend delete failed by no-op; leave file
                return
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "unlink", sticky_unlink):
            meta, path, tree, blockers, steps = capture_phase1_patch(
                worktree_path=self.repo,
                base_revision=self.base,
                artifacts_dir=self.artifacts,
                run_id="20260716T020408Z-a82843",
            )
        self.assertTrue(any(b.kind == "temp-index-retained" for b in blockers))


class ReadyAndDualConditionTests(unittest.TestCase):
    def test_compute_ready_requires_all(self) -> None:
        self.assertTrue(
            compute_integration_ready(
                terminal_outcome="completed",
                head_matches_base=True,
                scopes_ok=True,
                original_checkout_ok=True,
                sentinel_ok=True,
                patch_ok=True,
                validation_ok=True,
                build_gate_ok=True,
                shared_safety_ok=True,
                blockers=[],
                changed_count=1,
            )
        )
        self.assertFalse(
            compute_integration_ready(
                terminal_outcome="completed",
                head_matches_base=True,
                scopes_ok=True,
                original_checkout_ok=True,
                sentinel_ok=True,
                patch_ok=True,
                validation_ok=True,
                build_gate_ok=True,
                shared_safety_ok=True,
                blockers=[HandoffBlocker("no-changes", "x")],
                changed_count=1,
            )
        )

    def test_dual_condition_needs_envelope(self) -> None:
        doc = {
            "schemaVersion": 1,
            "runId": "20260716T020408Z-a82843",
            "taskId": "t1",
            "baseRevision": "a" * 40,
            "resultTreeOid": "b" * 40,
            "createdAtUtc": "2026-07-16T02:04:08Z",
            "changedFiles": [],
            "patch": {
                "format": "git-binary-full-index-v1",
                "relativePath": "artifacts/implementation.patch",
                "sha256": hashlib.sha256(b"p").hexdigest(),
                "bytes": 1,
            },
            "validation": {"requiredCommandsPassed": True, "buildGatePassed": True, "allPassed": True, "sources": {}},
            "integration": {"ready": True, "blockers": []},
            "worktree": {"retained": True, "path": "/t", "branch": "b"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(b"p")
            ready, blockers = dual_condition_ready(
                manifest=doc, envelope=None, patch_abs=p
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(b.get("kind") == "terminal-envelope-incomplete" for b in blockers)
            )
            ready2, _ = dual_condition_ready(
                manifest=doc,
                envelope={"status": "success", "runId": doc["runId"]},
                patch_abs=p,
            )
            self.assertTrue(ready2)

    def test_primary_mapping(self) -> None:
        cls, msg = primary_error_from_blockers(
            [HandoffBlocker("unexpected-commit", "moved")]
        )
        self.assertEqual(cls, "unexpected-commit")
        cls2, _ = primary_error_from_blockers(
            [HandoffBlocker("write-scope-violation", "out")]
        )
        self.assertEqual(cls2, "write-scope-violation")

    def test_step_order_constant(self) -> None:
        self.assertEqual(
            list(_STEP_ORDER),
            [
                "verify-sentinel",
                "remove-sentinel",
                "head-check",
                "changed-files",
                "write-scopes",
                "forensic-patch",
                "required-validation",
                "build-gate",
                "shared-safety",
                "terminal-outcome",
                "compute-ready",
                "write-manifest",
            ],
        )


class WriteManifestRoundTripTests(unittest.TestCase):
    def test_writer_reader_same_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "implementation-handoff.json"
            doc = {
                "schemaVersion": 1,
                "runId": "20260716T020408Z-a82843",
                "taskId": "t1",
                "baseRevision": "a" * 40,
                "resultTreeOid": "b" * 40,
                "createdAtUtc": "2026-07-16T02:04:08Z",
                "changedFiles": [],
                "patch": {
                    "format": "git-binary-full-index-v1",
                    "relativePath": "artifacts/implementation.patch",
                    "sha256": "d" * 64,
                    "bytes": 0,
                },
                "validation": {
                    "requiredCommandsPassed": True,
                    "buildGatePassed": True,
                    "allPassed": True,
                    "sources": {},
                },
                "integration": {"ready": False, "blockers": []},
                "worktree": {"retained": True, "path": "/t", "branch": "b"},
            }
            write_manifest(path, doc)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(validate_implementation_handoff(loaded), [])


if __name__ == "__main__":
    unittest.main()
