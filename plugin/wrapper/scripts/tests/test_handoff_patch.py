# wrapper/scripts/tests/test_handoff_patch.py

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
from groklib.handoff_patch import capture_phase1_patch, list_changed_paths
from groklib.implementation_handoff import HandoffBlocker
from tests import gitfixtures


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

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
            # clamp min is 1 MiB in code - set below by mocking _patch_max_bytes
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


class TapFailureLinesTests(unittest.TestCase):
    """tap_failure_lines surfaces failing test names from validation stdout tails."""

    def test_extracts_not_ok_lines_capped_and_redacted(self) -> None:
        from groklib.code_handoff_finalize import tap_failure_lines

        text = "\n".join(
            ["ok 1 - fine"]
            + ["not ok {} - failing test {}".format(i, i) for i in range(2, 10)]
            + ["# fail 8"]
        )
        lines = tap_failure_lines(text)
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(line.startswith("not ok") for line in lines))
        self.assertIn("failing test 2", lines[0])

    def test_empty_and_clean_text_yield_no_lines(self) -> None:
        from groklib.code_handoff_finalize import tap_failure_lines

        self.assertEqual(tap_failure_lines(""), [])
        self.assertEqual(tap_failure_lines("ok 1 - all good\n# pass 1"), [])
