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
from groklib.handoff_patch import capture_phase1_patch, list_changed_paths
from groklib.implementation_handoff import (
    compute_integration_ready,
    dual_condition_ready,
    primary_error_from_blockers,
    validate_implementation_handoff,
    write_manifest,
    HandoffBlocker,
    _STEP_ORDER,
)
from tests import gitfixtures


def _mini_patch(*paths: str) -> bytes:
    """Minimal unified patch with ``diff --git`` headers for dual-condition path cross-check."""
    if not paths:
        paths = ("a.ts",)
    parts = []
    for path in paths:
        parts.append(
            "diff --git a/{0} b/{0}\n"
            "index 1111111..2222222 100644\n"
            "--- a/{0}\n"
            "+++ b/{0}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n".format(path)
        )
    return "".join(parts).encode("utf-8")


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

    def test_changed_files_entries_must_be_shaped(self) -> None:
        doc = self._doc()
        doc["changedFiles"] = ["not-an-object"]
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("changedFiles[0]" in e for e in errs))
        doc["changedFiles"] = [{"path": "a.ts"}]  # missing status
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("status" in e for e in errs))
        doc["changedFiles"] = [{"path": "a.ts", "status": "renamed", "oldPath": None}]
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("oldPath" in e for e in errs))

    def test_ready_true_rejects_empty_changed_files(self) -> None:
        doc = self._doc()
        doc["changedFiles"] = []
        doc["integration"] = {"ready": True, "blockers": []}
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("changedFiles" in e for e in errs), errs)

    def test_ready_true_rejects_nonempty_blockers(self) -> None:
        doc = self._doc()
        doc["integration"] = {
            "ready": True,
            "blockers": [{"kind": "write-scope-violation", "message": "x"}],
        }
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("blockers" in e for e in errs), errs)

    def test_git_object_ids_must_be_full_hex(self) -> None:
        doc = self._doc()
        doc["baseRevision"] = "not-a-sha"
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("baseRevision" in e for e in errs), errs)
        doc = self._doc()
        doc["resultTreeOid"] = "abc"
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("resultTreeOid" in e for e in errs), errs)

    def test_ready_false_allows_empty_changed_files(self) -> None:
        doc = self._doc()
        doc["changedFiles"] = []
        doc["integration"] = {"ready": False, "blockers": [{"kind": "no-changes", "message": "x"}]}
        self.assertEqual(validate_implementation_handoff(doc), [])

    def test_ready_true_rejects_escaping_changed_paths(self) -> None:
        doc = self._doc()
        doc["changedFiles"] = [{"path": "../escape.ts", "status": "modified", "oldPath": None}]
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("path" in e and "repository-relative" in e for e in errs), errs)
        doc = self._doc()
        doc["changedFiles"] = [{"path": "/etc/passwd", "status": "modified", "oldPath": None}]
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("repository-relative" in e for e in errs), errs)
        doc = self._doc()
        doc["changedFiles"] = [
            {"path": "pkg/a.ts", "status": "renamed", "oldPath": "../../out.ts"}
        ]
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("oldPath" in e for e in errs), errs)

    def test_ready_true_rejects_failed_validation_flags(self) -> None:
        doc = self._doc()
        doc["validation"] = {
            "requiredCommandsPassed": False,
            "buildGatePassed": True,
            "allPassed": False,
            "sources": {},
        }
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("requiredCommandsPassed" in e for e in errs), errs)

    def test_ready_true_rejects_zero_byte_patch_with_changes(self) -> None:
        doc = self._doc()
        doc["patch"] = dict(doc["patch"], bytes=0, sha256=hashlib.sha256(b"").hexdigest())
        errs = validate_implementation_handoff(doc)
        self.assertTrue(any("patch.bytes" in e for e in errs), errs)
        # ready=false forensic stub still allows bytes 0
        doc["integration"] = {"ready": False, "blockers": [{"kind": "secret-material", "message": "x"}]}
        self.assertEqual(validate_implementation_handoff(doc), [])

    def test_changed_files_colon_filename_accepted(self) -> None:
        doc = self._doc()
        doc["changedFiles"] = [{"path": "a:b.txt", "status": "added", "oldPath": None}]
        self.assertEqual(validate_implementation_handoff(doc), [])

    def test_dual_condition_requires_code_envelope_mode(self) -> None:
        import tempfile

        doc = self._doc()
        patch_bytes = _mini_patch("a.ts")
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(patch_bytes)
            doc["patch"]["sha256"] = hashlib.sha256(patch_bytes).hexdigest()
            doc["patch"]["bytes"] = len(patch_bytes)
            ready, blockers = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "status",
                    "baseRevision": doc["baseRevision"],
                    "changedFiles": ["a.ts"],
                },
                patch_abs=p,
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(b.get("kind") == "terminal-envelope-incomplete" for b in blockers),
                blockers,
            )
            ready2, _ = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": doc["baseRevision"],
                    "changedFiles": ["a.ts"],
                },
                patch_abs=p,
            )
            self.assertTrue(ready2)

    def test_dual_condition_rejects_null_envelope_base(self) -> None:
        import tempfile

        doc = self._doc()
        patch_bytes = _mini_patch("a.ts")
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(patch_bytes)
            doc["patch"]["sha256"] = hashlib.sha256(patch_bytes).hexdigest()
            doc["patch"]["bytes"] = len(patch_bytes)
            ready, blockers = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": None,
                    "changedFiles": ["a.ts"],
                },
                patch_abs=p,
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(b.get("kind") == "terminal-envelope-incomplete" for b in blockers),
                blockers,
            )
            ready_empty, blockers_empty = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": "",
                    "changedFiles": ["a.ts"],
                },
                patch_abs=p,
            )
            self.assertFalse(ready_empty)
            self.assertTrue(
                any(b.get("kind") == "terminal-envelope-incomplete" for b in blockers_empty),
                blockers_empty,
            )

    def test_dual_condition_rejects_changed_files_mismatch_with_patch(self) -> None:
        import tempfile

        doc = self._doc()
        # Manifest claims a.ts; patch only touches b.ts
        patch_bytes = _mini_patch("b.ts")
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(patch_bytes)
            doc["patch"]["sha256"] = hashlib.sha256(patch_bytes).hexdigest()
            doc["patch"]["bytes"] = len(patch_bytes)
            ready, blockers = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": doc["baseRevision"],
                    "changedFiles": ["a.ts"],
                },
                patch_abs=p,
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(
                    b.get("kind") == "artifact-integrity-failure"
                    and "changedFiles" in (b.get("message") or "")
                    for b in blockers
                ),
                blockers,
            )

    def test_dual_condition_rejects_envelope_changed_files_mismatch(self) -> None:
        import tempfile

        doc = self._doc()
        patch_bytes = _mini_patch("a.ts")
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(patch_bytes)
            doc["patch"]["sha256"] = hashlib.sha256(patch_bytes).hexdigest()
            doc["patch"]["bytes"] = len(patch_bytes)
            ready, blockers = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": doc["baseRevision"],
                    "changedFiles": ["other.ts"],
                },
                patch_abs=p,
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(
                    b.get("kind") == "artifact-integrity-failure"
                    and "envelope changedFiles" in (b.get("message") or "")
                    for b in blockers
                ),
                blockers,
            )

    def test_dual_condition_rejects_empty_patch_bytes_even_if_hash_matches(self) -> None:
        import tempfile

        doc = self._doc()
        empty = b""
        # Corrupt ready=true with empty patch (validator would also reject; dual-condition
        # must fail closed if a bypassed manifest reaches it).
        doc["patch"]["sha256"] = hashlib.sha256(empty).hexdigest()
        doc["patch"]["bytes"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(empty)
            # Bypass manifest validator by calling dual_condition after forcing ready
            # (validate is called inside dual_condition - so this path hits validate first)
            ready, blockers = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": doc["baseRevision"],
                },
                patch_abs=p,
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(
                    b.get("kind") in ("handoff-unavailable", "artifact-integrity-failure")
                    for b in blockers
                ),
                blockers,
            )


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
        self.assertIn("durationSeconds", rec)
        self.assertNotIn("detail", rec)

    def test_redact_before_truncate_keeps_secret_marker(self) -> None:
        # Secret near the tail cut: if we sliced raw first, "Bearer " could fall
        # outside the window and leave the token body exposed. Redact full first.
        # Use non-token padding after the secret so the bearer pattern ends cleanly.
        pad = "n" * 5000
        secret = " Bearer abcdef0123456789deadbeefcafebabe "
        raw = (pad + secret + "!" * 200).encode("utf-8")
        rec = build_command_evidence(
            argv=["echo"],
            cwd="/tmp",
            purpose="t",
            exit_status=0,
            stdout=raw,
        )
        self.assertTrue(rec["stdoutTail"]["truncated"])
        self.assertNotIn("abcdef0123456789deadbeefcafebabe", rec["stdoutTail"]["text"])
        self.assertIn("redacted", rec["stdoutTail"]["text"].lower())

    def test_spawn_failure_record_is_envelope_valid(self) -> None:
        from groklib.envelope import failure_envelope, validate_envelope

        rec = build_command_evidence(
            argv=["missing-bin"],
            cwd="/tmp",
            purpose="contract-validation",
            exit_status=-1,
            duration_seconds=0.0,
        )
        env = failure_envelope(
            run_id="20260716T120000Z-abcdef",
            mode="code",
            error_class="validation-failure",
            message="requiredValidation could not run",
            commands=[rec],
        )
        self.assertEqual(validate_envelope(env), [])


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

    def test_patch_includes_unexpected_commit_vs_base(self) -> None:
        """When HEAD moved, patch must still be vs baseRevision (not live HEAD)."""
        (self.repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "unexpected")
        meta, path, tree, blockers, steps = capture_phase1_patch(
            worktree_path=self.repo,
            base_revision=self.base,
            artifacts_dir=self.artifacts,
            run_id="20260716T020408Z-a82843",
        )
        self.assertIsNotNone(meta)
        self.assertTrue(path and path.is_file())
        text = path.read_bytes()
        # Diff vs base must include the committed change content
        self.assertIn(b"v2", text)
        # Apply check against original base still works
        apply_repo = pathlib.Path(self.tmp) / "apply-base"
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
                "groklib.handoff_patch._patch_max_bytes", return_value=50
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
        patch_bytes = _mini_patch("a.ts")
        doc = {
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
                "sha256": hashlib.sha256(patch_bytes).hexdigest(),
                "bytes": len(patch_bytes),
            },
            "validation": {"requiredCommandsPassed": True, "buildGatePassed": True, "allPassed": True, "sources": {}},
            "integration": {"ready": True, "blockers": []},
            "worktree": {"retained": True, "path": "/t", "branch": "b"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "implementation.patch"
            p.write_bytes(patch_bytes)
            ready, blockers = dual_condition_ready(
                manifest=doc, envelope=None, patch_abs=p
            )
            self.assertFalse(ready)
            self.assertTrue(
                any(b.get("kind") == "terminal-envelope-incomplete" for b in blockers)
            )
            ready2, _ = dual_condition_ready(
                manifest=doc,
                envelope={
                    "status": "success",
                    "runId": doc["runId"],
                    "mode": "code",
                    "baseRevision": doc["baseRevision"],
                    "changedFiles": ["a.ts"],
                },
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

    def test_primary_skips_soft_blockers_before_hard(self) -> None:
        """Soft ready-only kinds must not steal primary from a later hard failure."""
        soft_then_hard = [
            HandoffBlocker("temp-index-retained", "index left"),
            HandoffBlocker("no-changes", "empty"),
            HandoffBlocker(
                "unexpected-edits",
                "escape",
                detail={"phase": "post-build-gate", "violations": ["/x"]},
            ),
        ]
        cls, msg = primary_error_from_blockers(soft_then_hard)
        self.assertEqual(cls, "unexpected-edits")
        self.assertEqual(msg, "escape")
        # Soft-only list → no primary (ready false, code envelope can still succeed)
        soft_only = [
            HandoffBlocker("temp-index-retained", "index left"),
            HandoffBlocker("no-changes", "empty"),
        ]
        self.assertEqual(primary_error_from_blockers(soft_only), (None, None))

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
                "forensic-patch-post-gate",
                "shared-safety",
                "terminal-outcome",
                "compute-ready",
                "write-manifest",
            ],
        )

    def test_unknown_blocker_kind_is_hard_primary(self) -> None:
        cls, msg = primary_error_from_blockers(
            [HandoffBlocker("brand-new-kind", "unexpected")]
        )
        self.assertEqual(cls, "artifact-generation-failure")
        self.assertIn("unexpected", msg)


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

    def test_write_manifest_redacts_secret_argv_in_blockers(self) -> None:
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
                    "requiredCommandsPassed": False,
                    "buildGatePassed": True,
                    "allPassed": False,
                    "sources": {},
                },
                "integration": {
                    "ready": False,
                    "blockers": [
                        {
                            "kind": "validation-failure",
                            "message": "failed",
                            "detail": {
                                "argv": ["tool", "Bearer abcdef0123456789deadbeefcafebabe"],
                            },
                        }
                    ],
                },
                "worktree": {"retained": True, "path": "/t", "branch": "b"},
            }
            write_manifest(path, doc)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("abcdef0123456789deadbeefcafebabe", text)
            self.assertIn("redacted", text.lower())


class PostGatePatchClearTests(unittest.TestCase):
    """When post-gate capture is rejected, pre-gate patch metadata must not survive."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-postgate-")
        self.repo = gitfixtures.make_repo(self.tmp)
        (self.repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        (self.repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
        self.run_dir = pathlib.Path(self.tmp) / "run"
        self.artifacts = self.run_dir / "artifacts"
        self.artifacts.mkdir(parents=True)
        # Pre-gate patch file that would be stale after a rejected recapture
        stale = self.artifacts / "implementation.patch"
        stale.write_bytes(b"STALE-PRE-GATE-PATCH")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_gate_secret_material_clears_pre_gate_patch_meta(self) -> None:
        from groklib.code_handoff_finalize import code_handoff_finalize
        from groklib.worktree import ExternalWorktree
        from groklib.modes._worktree import FinalizeStage, WorktreeAccumulator

        pre_meta = {
            "format": "git-binary-full-index-v1",
            "relativePath": "artifacts/implementation.patch",
            "sha256": hashlib.sha256(b"STALE-PRE-GATE-PATCH").hexdigest(),
            "bytes": len(b"STALE-PRE-GATE-PATCH"),
        }
        post_tree = "c" * 40
        pre_tree = "d" * 40

        call_n = {"n": 0}

        def fake_capture(**_kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return (
                    pre_meta,
                    self.artifacts / "implementation.patch",
                    pre_tree,
                    [],
                    ["phase1-pre"],
                )
            # Post-gate: fatal secret-material; no meta, but write-tree reached
            return (
                None,
                None,
                post_tree,
                [HandoffBlocker("secret-material", "secret-shaped material in patch", {})],
                ["phase1-post"],
            )

        wt = ExternalWorktree(
            path=self.repo,
            branch="grok/code/test",
            base_revision=self.base,
            repo_root=self.repo,
        )
        acc = WorktreeAccumulator()
        stage = FinalizeStage(
            result=mock.Mock(),
            worktree=wt,
            effective_model="test",
            progress=mock.Mock(),
            acc=acc,
            run_id="20260716T020408Z-a82843",
        )

        def _ok(*_a, **_k):
            return None

        def _recorded(**_k):
            return {"exitStatus": 0}

        with mock.patch(
            "groklib.code_handoff_finalize.capture_phase1_patch", side_effect=fake_capture
        ):
            try:
                result = code_handoff_finalize(
                    stage=stage,
                    sentinel_name=".__grok_sentinel_never__",
                    contract=None,
                    artifacts_dir=self.artifacts,
                    original_baseline=None,
                    run_build_gate=_ok,
                    assert_changes_within=_ok,
                    assert_original_checkout_unmodified=_ok,
                    assert_cwd_sentinel=_ok,
                    run_recorded_command=_recorded,
                )
            except GrokWrapperError:
                # secret-material is hard; finalize may raise after writing
                manifest_path = self.run_dir / "implementation-handoff.json"
                self.assertTrue(manifest_path.is_file())
                doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            else:
                doc = result.manifest

        self.assertIsNotNone(doc)
        self.assertFalse(doc["integration"]["ready"])
        # Stub patch, not the pre-gate secret-free patch metadata
        self.assertEqual(doc["patch"]["bytes"], 0)
        self.assertEqual(doc["patch"]["sha256"], "0" * 64)
        # Prefer post-gate tree when capture reached write-tree
        self.assertEqual(doc["resultTreeOid"], post_tree)
        kinds = [b.get("kind") for b in doc["integration"]["blockers"]]
        self.assertIn("secret-material", kinds)
        # Disk must not retain pre-gate patch bytes under the advertised path
        self.assertFalse((self.artifacts / "implementation.patch").is_file())


class ListChangedPathsFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-listchg-")
        self.repo = gitfixtures.make_repo(self.tmp)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_git_diff_fatal_raises(self) -> None:
        with mock.patch(
            "groklib.handoff_patch._run_git_env",
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=128, stdout=b"", stderr=b"fatal: bad object"
            ),
        ):
            with self.assertRaises(GrokWrapperError) as cm:
                list_changed_paths(self.repo, self.base)
            self.assertEqual(cm.exception.error_class, "artifact-generation-failure")


class Phase1SecretScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-secpatch-")
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

    def test_bearer_in_file_blocks_patch_write(self) -> None:
        # Split literal so fixtures do not hold contiguous secret-shaped tokens.
        token = "Bearer " + "abcdef0123456789" + "deadbeefcafebabe"
        (self.repo / "secret.txt").write_text("key=" + token + "\n", encoding="utf-8")
        meta, path, tree, blockers, steps = capture_phase1_patch(
            worktree_path=self.repo,
            base_revision=self.base,
            artifacts_dir=self.artifacts,
            run_id="20260716T020408Z-a82843",
        )
        self.assertIsNone(meta)
        self.assertIsNone(path)
        self.assertTrue(any(b.kind == "secret-material" for b in blockers))
        self.assertFalse((self.artifacts / "implementation.patch").is_file())

    def test_binary_patch_bytes_scan_catches_embedded_bearer(self) -> None:
        # Git may encode binary files without leaving raw ASCII in the patch
        # body; the scanner itself must still catch secrets in raw byte streams.
        from groklib.handoff_patch import scan_patch_bytes_for_secrets
        from groklib.envelope import SecretMaterialError

        token = b"Bearer " + b"abcdef0123456789" + b"deadbeefcafebabe"
        blob = b"\x00\x01" + token + b"\xff\xfe"
        with self.assertRaises(SecretMaterialError):
            scan_patch_bytes_for_secrets(blob)
        # UTF-8 replace path is weaker; latin-1 path is what production uses.
        scan_patch_bytes_for_secrets(b"hello without credentials")


if __name__ == "__main__":
    unittest.main()
