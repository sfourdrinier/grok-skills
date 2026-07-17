# wrapper/scripts/tests/test_mode_peer_finalize.py
#
# Split from test_mode_peer.py (900-line cap); subclasses PeerTestBase.

import os
import json
import pathlib
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

import shutil
from groklib import GrokWrapperError
from groklib import envelope as envelope_mod
from groklib import platformsupport
from groklib import runstate
from groklib.modes import peer as peer_mod

from tests import gitfixtures
from tests.peer_test_base import PeerTestBase, _FakeAcpClient, _FakeChild, _split_bearer_fixture


class PeerFinalizeTests(PeerTestBase):
    def test_peer_stop_labels_confinement_and_destroys_home(self) -> None:
        from groklib.modes import peer_finalize

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            result = peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=None,
                original_baseline=baseline,
                stage=stage,
            )
        manifest_path = run_paths.run_dir / "implementation-handoff.json"
        self.assertTrue(manifest_path.is_file(), "finalize must write handoff artifacts")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("confinement"), "worktree-final-diff-only")
        self.assertFalse(pathlib.Path(home.home_dir).exists(), "private home must be destroyed")
        self.assertEqual(result.get("status"), "success")

    def test_peer_required_validation_fail_not_ready(self) -> None:
        """requiredValidation that FAILS -> not ready with validation-failure evidence."""
        from groklib.modes import peer_finalize

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        self._plant_worktree_change(wt)
        contract = {
            "schemaVersion": 1,
            "taskId": "peer-fail-val",
            "target": "pkg",
            "objective": "fail validation",
            "acceptanceCriteria": ["must fail"],
            "writeScopes": [{"kind": "subtree", "path": "pkg"}],
            "requiredValidation": [
                {"argv": ["false"], "cwd": ".", "purpose": "must-fail"},
            ],
        }
        peer_doc["contract"] = contract
        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=contract,
                original_baseline=baseline,
                stage=stage,
            )
        manifest = json.loads(
            (run_paths.run_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        self.assertIs(manifest["integration"]["ready"], False)
        self.assertTrue(
            any(b.get("kind") == "validation-failure" for b in manifest["integration"]["blockers"]),
            manifest["integration"]["blockers"],
        )
        sources = manifest["validation"]["sources"]
        self.assertIs(sources["contractRequiredValidation"]["authoritative"], True)
        self.assertIs(sources["contractRequiredValidation"]["passed"], False)
        # Real evidence: non-zero exitStatus recorded (never forged 0)
        cmds = stage.acc.commands or []
        self.assertTrue(cmds, "requiredValidation must run and record commands")
        self.assertTrue(any(int(c.get("exitStatus", 0)) != 0 for c in cmds), cmds)

    def test_peer_required_validation_pass_ready(self) -> None:
        """requiredValidation that PASSES -> ready with authoritative exit-0 evidence."""
        from groklib.modes import peer_finalize

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        self._plant_worktree_change(wt)
        contract = {
            "schemaVersion": 1,
            "taskId": "peer-pass-val",
            "target": "pkg",
            "objective": "pass validation",
            "acceptanceCriteria": ["must pass"],
            "writeScopes": [{"kind": "subtree", "path": "pkg"}],
            "requiredValidation": [
                {"argv": ["true"], "cwd": ".", "purpose": "must-pass"},
            ],
        }
        peer_doc["contract"] = contract
        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            result = peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=contract,
                original_baseline=baseline,
                stage=stage,
            )
        manifest = json.loads(
            (run_paths.run_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        self.assertIs(manifest["integration"]["ready"], True, manifest.get("integration"))
        self.assertEqual(manifest.get("confinement"), "contract-scopes")
        sources = manifest["validation"]["sources"]
        self.assertIs(sources["contractRequiredValidation"]["authoritative"], True)
        self.assertIs(sources["contractRequiredValidation"]["passed"], True)
        cmds = stage.acc.commands or []
        self.assertTrue(
            any(int(c.get("exitStatus", 1)) == 0 for c in cmds),
            "ready requires a real exit-0 commands[] entry: {}".format(cmds),
        )
        self.assertIs(result.get("response", {}).get("peer", {}).get("integrationReady"), True)

    def test_peer_no_contract_no_gate_not_ready(self) -> None:
        """No contract + no build gate (non-JS) -> honest not-ready."""
        from groklib.modes import peer_finalize

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        self._plant_worktree_change(wt)
        peer_doc["projectPackageManager"] = None
        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=None,
                original_baseline=baseline,
                stage=stage,
            )
        manifest = json.loads(
            (run_paths.run_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        self.assertIs(manifest["integration"]["ready"], False)
        blockers = manifest["integration"]["blockers"]
        self.assertTrue(
            any(b.get("kind") == "no-authoritative-validation" for b in blockers),
            blockers,
        )
        sources = manifest["validation"]["sources"]
        self.assertIs(sources["wrapperBuildGate"]["authoritative"], False)
        self.assertIs(sources["contractRequiredValidation"]["authoritative"], False)

    def test_peer_no_contract_build_gate_pass_ready(self) -> None:
        """No contract + build gate passes (JS repo) -> ready."""
        from groklib.modes import peer_finalize

        # Commit package.json under the peer target (pkg/) BEFORE creating the
        # worktree so the base revision has gate scripts (D1(b) via git show).
        pkg = {
            "name": "peer-gate-fixture",
            "scripts": {"build": "true", "test": "true"},
        }
        (self.repo / "pkg" / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        gitfixtures._git(self.repo, ["add", "pkg/package.json"])
        gitfixtures._git(self.repo, ["commit", "-q", "-m", "add pkg package.json for gate"])
        self.base = gitfixtures.head_revision(self.repo)

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        peer_doc["projectPackageManager"] = "npm"
        peer_doc["targetRelative"] = "pkg"
        self.assertTrue(
            (wt.path / "pkg" / "package.json").is_file(),
            "worktree must inherit pkg/package.json from base",
        )
        self._plant_worktree_change(wt)
        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            result = peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=None,
                original_baseline=baseline,
                stage=stage,
            )
        manifest = json.loads(
            (run_paths.run_dir / "implementation-handoff.json").read_text(encoding="utf-8")
        )
        cmds = stage.acc.commands or []
        gate_cmds = [
            c for c in cmds if str(c.get("purpose") or "").startswith("build-gate:")
        ]
        self.assertTrue(gate_cmds, "build gate must run: cmds={}".format(cmds))
        self.assertIs(
            manifest["integration"]["ready"],
            True,
            "expected ready; integration={}".format(manifest.get("integration")),
        )
        self.assertIs(
            manifest["validation"]["sources"]["wrapperBuildGate"]["authoritative"], True
        )
        self.assertTrue(all(int(c.get("exitStatus", 1)) == 0 for c in gate_cmds), gate_cmds)
        self.assertIs(result.get("response", {}).get("peer", {}).get("integrationReady"), True)

    def test_peer_start_records_worktree_on_run_json(self) -> None:
        """Finding 4: peer-start CAS-updates worktreePath/lifecycle like code mode."""
        source = pathlib.Path(self.tmp_root) / "grok" / ".grok"
        source.mkdir(parents=True)
        (source / "auth.json").write_text("{}\n", encoding="utf-8")

        ns = mock.Mock(
            target=str(self.repo / "pkg"),
            base=self.base,
            contract_file=None,
            model="grok-4.5",
            web=None,
            timeout=60,
            max_turns=None,
            grok_binary=pathlib.Path("/usr/bin/true"),
            task="hello",
            task_file=None,
        )

        def _serve_once(session, running_env, preopened=None):
            if preopened is not None:
                try:
                    preopened.close()
                except Exception:
                    pass
            return envelope_mod.build_envelope(
                run_id=session.run_id,
                mode="peer-stop",
                status="success",
                response={"stopped": True},
            )

        with self._patch_spawn_and_acp():
            with mock.patch.object(peer_mod, "_serve_control_plane", side_effect=_serve_once):
                with mock.patch.object(peer_mod, "require_probed_platform_for_live", return_value=None):
                    with mock.patch.object(peer_mod, "check_version", return_value="0.0.0"):
                        env = peer_mod.run_peer_start(ns)
        run_id = env["runId"] if env.get("runId") else None
        # serve returns peer-stop env; running was emitted earlier - recover run id from peer.json
        runs = list((runstate.state_root() / "runs").iterdir())
        self.assertTrue(runs)
        run_id = runs[0].name
        record = runstate.load_run_record(run_id)
        self.assertEqual(record.get("lifecycle"), "running")
        self.assertIsNotNone(record.get("worktreePath"))
        self.assertTrue(pathlib.Path(record["worktreePath"]).is_dir())
        self.assertIsNotNone(record.get("worktreeBranch"))
        self.assertIsNotNone(record.get("baseRevision"))
        peer_doc = json.loads(
            (runstate.state_root() / "runs" / run_id / "peer.json").read_text(encoding="utf-8")
        )
        self.assertIn("originalBaseline", peer_doc)
        self.assertIsInstance(peer_doc["originalBaseline"], dict)

    def test_peer_stop_terminalizes_and_cleanup_removes_worktree(self) -> None:
        """Finding 4: peer-stop terminalize + cleanup --confirm removes worktree."""
        from groklib.modes import peer_finalize
        from groklib.modes import cleanup as cleanup_mod

        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=None,
                original_baseline=baseline,
                stage=stage,
            )
        record = runstate.load_run_record(run_paths.run_id)
        self.assertIn(record.get("lifecycle"), ("completed", "failed"))
        self.assertTrue(wt.path.is_dir(), "worktree retained until cleanup")

        ns = mock.Mock(run_id=run_paths.run_id, confirm=True)
        out = cleanup_mod.run(ns)
        self.assertEqual(out.get("status"), "success")
        self.assertFalse(wt.path.exists(), "cleanup must remove peer worktree")
        self.assertFalse(run_paths.run_dir.exists(), "cleanup must remove run dir")

    def test_peer_crash_stop_uses_start_baseline_never_recapture(self) -> None:
        """Finding 5: crash-path peer-stop loads originalBaseline; no re-capture."""
        home, run_paths, wt, peer_doc, stage, baseline = self._peer_finalize_fixture(
            plant_sandbox=True
        )
        # No control socket: forces local finalize path.
        ns = mock.Mock(run_id=run_paths.run_id)
        captured = {}

        def _fake_finalize(**kwargs):
            captured["baseline"] = kwargs.get("original_baseline")
            return envelope_mod.build_envelope(
                run_id=run_paths.run_id,
                mode="peer-stop",
                status="success",
                response={"ok": True},
            )

        with mock.patch(
            "groklib.modes.peer_finalize.finalize_peer_session", side_effect=_fake_finalize
        ):
            with mock.patch.object(
                peer_mod.worktree_escape,
                "capture_original_checkout_baseline",
                side_effect=AssertionError("must not re-capture baseline at stop"),
            ):
                env = peer_mod.run_peer_stop(ns)
        self.assertEqual(env["status"], "success")
        self.assertEqual(captured["baseline"], baseline)

    def test_wrapper_acp_default_works_without_experimental_flag(self) -> None:
        """Task 7.4: GROK_EXPERIMENTAL_ACP unset is no longer a hard gate."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROK_EXPERIMENTAL_ACP", None)
            os.environ.pop("GROK_DISABLE_ACP", None)
            # Gate alone must not raise usage-error; deeper failures are ok.
            from groklib.modes import peer_control

            peer_control.require_experimental_acp()  # must not raise

    def test_wrapper_refuses_when_acp_disabled(self) -> None:
        """Task 7.4: GROK_DISABLE_ACP=1 refuses peer modes (opt-out)."""
        with mock.patch.dict(os.environ, {"GROK_DISABLE_ACP": "1"}, clear=False):
            for mode_fn, ns in (
                (peer_mod.run_peer_start, mock.Mock(target="x", base="y")),
                (peer_mod.run_peer_prompt, mock.Mock(run_id="x", task="t", task_file=None)),
                (peer_mod.run_peer_stop, mock.Mock(run_id="x")),
            ):
                with self.assertRaises(GrokWrapperError) as ctx:
                    mode_fn(ns)
                self.assertEqual(ctx.exception.error_class, "usage-error")
                self.assertIn("GROK_DISABLE_ACP", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
