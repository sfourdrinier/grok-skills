# wrapper/scripts/tests/test_mode_direct.py
#
# Hardened-direct mode (code --integration direct, the new default): Grok edits
# the real operator checkout under sandbox + post-run deny/scope/dirty guards.
# Security tests are the point (deny-glob, escape, scope, dirty-overlap,
# verify_enforcement grant-coverage).

import argparse
import contextlib
import io
import json
import os
import pathlib
import tempfile
from typing import Callable, Dict, List, Optional
from unittest import mock

import grok_agent
from groklib import GrokWrapperError
from groklib import runstate
from groklib import sandbox as sandbox_mod
from groklib.modes import _direct
from groklib.modes import code as code_mode
from groklib.modes import direct as direct_mode

from tests.modefixtures import ModeHarness, _passing_sandbox_event
from tests.worktreefixtures import WorktreeModeHarness


class DirectModeHarness(WorktreeModeHarness):
    """Drive code --integration direct against a real git repo; plant into the REPO root."""

    def drive_direct(
        self,
        argv: List[str],
        *,
        repo_root: pathlib.Path,
        scenario: str = "ok-json",
        control_extra: Optional[Dict[str, str]] = None,
        plant: Optional[Callable[[pathlib.Path, str], None]] = None,
        env_extra: Optional[Dict[str, str]] = None,
        extra_patchers: Optional[List] = None,
        sandbox_grants: Optional[List[str]] = None,
        sandbox_profile: Optional[str] = None,
    ):
        """Like WorktreeModeHarness.drive but no worktree inject; plant targets repo_root."""
        argv = list(argv)
        if "--integration" not in argv:
            # Keep explicit direct (or default) - never inject worktree.
            pass
        if sandbox_profile is None:
            sandbox_profile = sandbox_mod.custom_profile_name("direct")
        real_create = _direct.create_private_home

        def _patched_create(**kwargs):
            home = real_create(**kwargs)
            control: Dict[str, str] = {"scenario": scenario, "argvLog": str(self.argv_log_path)}
            if control_extra:
                control.update(control_extra)
            (home.home_dir / "fake-grok-control.json").write_text(
                json.dumps(control), encoding="utf-8"
            )
            private_tmp = home.home_dir / "tmp"
            if sandbox_grants is not None:
                grants = list(sandbox_grants)
            else:
                grants = [str(repo_root.resolve()), str(private_tmp.resolve())]
            (home.grok_dir / "sandbox-events.jsonl").write_text(
                json.dumps(_passing_sandbox_event(sandbox_profile, read_write_paths=grants)) + "\n",
                encoding="utf-8",
            )
            if plant is not None:
                # Resolve the NEW run id from the runs dir (mode=direct, running).
                run_id = self._active_direct_run_id()
                plant(repo_root, run_id)
            return home

        patchers = [
            mock.patch.object(_direct, "create_private_home", _patched_create),
            mock.patch.object(_direct, "source_grok_dir", lambda: self.source_grok),
        ]
        if extra_patchers:
            patchers.extend(extra_patchers)

        buffer = io.StringIO()
        original_cwd = os.getcwd()
        with contextlib.ExitStack() as stack:
            env = {"GROK_PACKAGE_MANAGER_BINARY": str(self.pnpm_binary)}
            if env_extra:
                env.update(env_extra)
            stack.enter_context(mock.patch.dict(os.environ, env))
            for patcher in patchers:
                stack.enter_context(patcher)
            os.chdir(str(pathlib.Path(repo_root)))
            try:
                with contextlib.redirect_stdout(buffer):
                    exit_code = grok_agent.main(argv)
            finally:
                os.chdir(original_cwd)
        return exit_code, buffer.getvalue()

    def _active_direct_run_id(self) -> str:
        runs_root = runstate.state_root() / "runs"
        for child in sorted(runs_root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            try:
                rec = runstate.load_run_record(child.name)
            except Exception:
                continue
            if rec.get("mode") == "direct" and rec.get("lifecycle") in (
                "running",
                "created",
                "finalizing",
            ):
                return child.name
        raise AssertionError("could not resolve active direct run id")

    def _direct_argv(self, *extra: str) -> List[str]:
        return [
            "code",
            "--integration",
            "direct",
            "--target",
            "pkg",
            "--base",
            "HEAD",
            "--task",
            "Edit the real tree",
            *extra,
        ]


class DirectModeSecurityTests(DirectModeHarness):
    """Security-critical post-run guards for hardened-direct."""

    def test_deny_glob_env_fails_protected_path_write(self) -> None:
        repo = self.make_code_repo()

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / ".env").write_text("SECRET=shhh\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "protected-path-write")
        self.assertIn(".env", json.dumps(env["error"]))

    def test_deny_glob_git_config_fails_protected_path_write(self) -> None:
        repo = self.make_code_repo()

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            git_config = repo_root / ".git" / "config"
            # Append so the fingerprint of .git/config changes.
            with git_config.open("a", encoding="utf-8") as handle:
                handle.write("\n# grok-direct-test\n")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "protected-path-write")
        detail = json.dumps(env["error"])
        self.assertTrue(".git" in detail or "config" in detail, detail)

    def test_deny_glob_pem_fails_protected_path_write(self) -> None:
        repo = self.make_code_repo()

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "pkg" / "cert.pem").write_text("-----BEGIN FAKE-----\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "protected-path-write")
        self.assertIn("cert.pem", json.dumps(env["error"]))

    def test_escape_above_repo_via_symlink_fails_closed(self) -> None:
        repo = self.make_code_repo()
        outside = pathlib.Path(self.tmp_root) / "outside-escape"
        outside.mkdir()
        target = outside / "payload.txt"
        target.write_text("before\n", encoding="utf-8")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            link = repo_root / "escape-link"
            if not link.exists():
                link.symlink_to(target)
            target.write_text("after-escape\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        # realpath-under-repo or sandbox/protected fail-closed
        self.assertIn(
            env["error"]["class"],
            ("sandbox-failure", "protected-path-write", "unexpected-edits"),
            out,
        )

    def test_scope_violation_fails_write_scope_violation(self) -> None:
        repo = self.make_code_repo()
        contract = {
            "schemaVersion": 1,
            "taskId": "direct-scope",
            "objective": "only touch pkg/mod.txt",
            "acceptanceCriteria": ["mod only"],
            "target": "pkg",
            "writeScopes": [{"kind": "file", "path": "pkg/mod.txt"}],
            "requiredValidation": [],
        }
        contract_path = pathlib.Path(self.tmp_root) / "contract.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "a.txt").write_text("touched outside scope\n", encoding="utf-8")

        exit_code, out = self.drive_direct(
            self._direct_argv("--contract-file", str(contract_path)),
            repo_root=repo,
            plant=_plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "write-scope-violation")

    def test_ignored_byproduct_outside_scopes_not_scope_violation(self) -> None:
        """Gitignored build byproducts outside writeScopes must not fail scope (7.1c)."""
        repo = self.make_code_repo()
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore")
        self._git(repo, "commit", "-q", "-m", "ignore pycache byproducts")
        contract = {
            "schemaVersion": 1,
            "taskId": "direct-byproduct-scope",
            "objective": "only touch pkg/mod.txt",
            "acceptanceCriteria": ["mod only"],
            "target": "pkg",
            "writeScopes": [{"kind": "file", "path": "pkg/mod.txt"}],
            "requiredValidation": [],
        }
        contract_path = pathlib.Path(self.tmp_root) / "contract-byproduct.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            # In-scope legitimate edit plus an ignored byproduct OUTSIDE scopes
            # (simulates validation/build writing __pycache__ outside the contract).
            (repo_root / "pkg" / "mod.txt").write_text("module fixed\n", encoding="utf-8")
            pycache = repo_root / "other" / "__pycache__"
            pycache.mkdir(parents=True, exist_ok=True)
            (pycache / "_direct.cpython-314.pyc").write_bytes(b"\0bytecode\0")

        exit_code, out = self.drive_direct(
            self._direct_argv("--contract-file", str(contract_path)),
            repo_root=repo,
            plant=_plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")
        self.assertNotEqual(env.get("error", {}).get("class"), "write-scope-violation")

    def test_source_outside_scopes_still_write_scope_violation(self) -> None:
        """Genuine source outside writeScopes must still fail (scope enforcement intact)."""
        repo = self.make_code_repo()
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore")
        self._git(repo, "commit", "-q", "-m", "ignore pycache byproducts")
        contract = {
            "schemaVersion": 1,
            "taskId": "direct-source-scope",
            "objective": "only touch pkg/mod.txt",
            "acceptanceCriteria": ["mod only"],
            "target": "pkg",
            "writeScopes": [{"kind": "file", "path": "pkg/mod.txt"}],
            "requiredValidation": [],
        }
        contract_path = pathlib.Path(self.tmp_root) / "contract-source-scope.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "a.txt").write_text("genuine source outside scope\n", encoding="utf-8")

        exit_code, out = self.drive_direct(
            self._direct_argv("--contract-file", str(contract_path)),
            repo_root=repo,
            plant=_plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "write-scope-violation")
        self.assertIn("a.txt", json.dumps(env["error"]))

    def test_ignored_byproduct_operator_dirty_not_dirty_path_conflict(self) -> None:
        """Operator-dirty gitignored byproduct modified by Grok is not dirty-path-conflict."""
        repo = self.make_code_repo()
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore")
        self._git(repo, "commit", "-q", "-m", "ignore pycache byproducts")
        # Pre-existing operator dirt that is gitignored (build byproduct).
        pycache = repo / "__pycache__"
        pycache.mkdir(parents=True, exist_ok=True)
        (pycache / "pre.cpython-314.pyc").write_bytes(b"operator-v1")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "__pycache__" / "pre.cpython-314.pyc").write_bytes(b"grok-v2")
            (repo_root / "pkg" / "mod.txt").write_text("module fixed\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")
        self.assertNotEqual(env.get("error", {}).get("class"), "dirty-path-conflict")

    def test_env_ignored_and_deny_still_protected_path_write(self) -> None:
        """Deny scan still catches .env even when it is also gitignored (7.1c)."""
        repo = self.make_code_repo()
        (repo / ".gitignore").write_text(".env\n.env.*\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore")
        self._git(repo, "commit", "-q", "-m", "ignore env files")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / ".env").write_text("SECRET=shhh\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "protected-path-write")
        self.assertIn(".env", json.dumps(env["error"]))

    def test_dirty_overlap_fails_without_force(self) -> None:
        repo = self.make_code_repo()
        # make_repo leaves dirty.txt untracked; also dirtied here for clarity.
        dirty = repo / "dirty.txt"
        dirty.write_text("operator dirt v1\n", encoding="utf-8")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "dirty.txt").write_text("grok also touched\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "dirty-path-conflict")
        self.assertIn("dirty.txt", json.dumps(env["error"]))
        self.assertIn("--force", json.dumps(env["error"]))

    def test_dirty_overlap_allowed_with_force(self) -> None:
        repo = self.make_code_repo()
        dirty = repo / "dirty.txt"
        dirty.write_text("operator dirt v1\n", encoding="utf-8")

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "dirty.txt").write_text("grok also touched\n", encoding="utf-8")
            # Also edit a clean tracked file so the run has a legitimate change.
            (repo_root / "pkg" / "mod.txt").write_text("module fixed\n", encoding="utf-8")

        exit_code, out = self.drive_direct(
            self._direct_argv("--force"),
            repo_root=repo,
            plant=_plant,
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")
        self.assertIn("dirty.txt", env.get("changedFiles") or [])

    def test_happy_path_edits_real_tree_no_worktree(self) -> None:
        repo = self.make_code_repo()
        preexisting = self._worktree_dirs()

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "pkg" / "mod.txt").write_text("module fixed by direct\n", encoding="utf-8")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(env["status"], "success")
        self.assertEqual(env["mode"], "direct")
        self.assertIsNone(env.get("worktreePath"))
        self.assertIn("pkg/mod.txt", env.get("changedFiles") or [])
        self.assertEqual(
            (repo / "pkg" / "mod.txt").read_text(encoding="utf-8"),
            "module fixed by direct\n",
        )
        self.assertEqual(self._worktree_dirs(), preexisting, "direct must not create a worktree")
        # run.json integration=direct, worktreePath null
        run_id = env["runId"]
        record = runstate.load_run_record(run_id)
        self.assertEqual(record.get("integration"), "direct")
        self.assertIsNone(record.get("worktreePath"))

    def test_verify_enforcement_hard_fails_on_grant_coverage_miss(self) -> None:
        repo = self.make_code_repo()

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "pkg" / "mod.txt").write_text("module\n", encoding="utf-8")

        # Grant only private_tmp (empty list replaced at plant time): omit repo_root.
        # Use a custom plant that rewrites grants after home create - simpler: pass
        # sandbox_grants that only include a decoy path so grant-coverage misses.
        decoy = pathlib.Path(self.tmp_root) / "decoy-writable"
        decoy.mkdir()

        exit_code, out = self.drive_direct(
            self._direct_argv(),
            repo_root=repo,
            plant=_plant,
            sandbox_grants=[str(decoy.resolve())],
        )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "sandbox-failure")
        self.assertEqual(env["status"], "failure")


class DirectPolicyAndCliTests(DirectModeHarness):
    """policy_for_mode + CLI default / worktree routing."""

    def test_policy_for_mode_direct_writable_roots(self) -> None:
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="direct-policy-", dir=self.tmp_root))
        repo_root = scratch / "repo"
        private_tmp = scratch / "tmp"
        repo_root.mkdir()
        private_tmp.mkdir()
        policy = sandbox_mod.policy_for_mode(
            "direct",
            worktree=None,
            private_tmp=private_tmp,
            repo_root=repo_root,
        )
        self.assertEqual(policy.mode, "direct")
        self.assertEqual(policy.profile, "grok-skills-direct")
        self.assertEqual(
            policy.writable_roots,
            (str(repo_root.resolve()), str(private_tmp.resolve())),
        )
        self.assertFalse(policy.secret_read_denial_proven)

    def test_policy_for_mode_direct_missing_repo_root_is_usage_error(self) -> None:
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="direct-policy-", dir=self.tmp_root))
        private_tmp = scratch / "tmp"
        private_tmp.mkdir()
        with self.assertRaises(GrokWrapperError) as caught:
            sandbox_mod.policy_for_mode("direct", worktree=None, private_tmp=private_tmp)
        self.assertEqual(caught.exception.error_class, "usage-error")

    def test_code_integration_defaults_to_direct(self) -> None:
        parser = grok_agent._build_parser()
        args = parser.parse_args(
            ["code", "--target", "pkg", "--base", "HEAD", "--task", "x"]
        )
        self.assertEqual(args.integration, "direct")
        self.assertFalse(args.force)

    def test_integration_worktree_routes_to_run_worktree_mode(self) -> None:
        repo = self.make_code_repo()
        called = {"worktree": False, "direct": False}

        real_worktree = code_mode.run_worktree_mode
        real_direct = direct_mode.run_direct_mode

        def _wt(*args, **kwargs):
            called["worktree"] = True
            return real_worktree(*args, **kwargs)

        def _dir(*args, **kwargs):
            called["direct"] = True
            return real_direct(*args, **kwargs)

        def _plant_sentinel(worktree_path: pathlib.Path, run_id: str) -> None:
            (worktree_path / (".grok-run-" + run_id)).write_text("", encoding="utf-8")

        exit_code, out = self.drive(
            [
                "code",
                "--integration",
                "worktree",
                "--target",
                "pkg",
                "--base",
                "HEAD",
                "--task",
                "Fix",
            ],
            repo_root=repo,
            plant=_plant_sentinel,
            extra_patchers=[
                mock.patch.object(code_mode, "run_worktree_mode", _wt),
            ],
        )
        self.assertEqual(exit_code, 0, out)
        env = json.loads(out)
        self.assertEqual(env["mode"], "code")
        self.assertIsNotNone(env.get("worktreePath"))
        self.assertTrue(called["worktree"])

    def test_default_argv_without_integration_uses_direct_runner(self) -> None:
        repo = self.make_code_repo()
        called = {"direct": False}
        real_run = direct_mode.run

        def _spy(args: argparse.Namespace):
            called["direct"] = True
            self.assertEqual(getattr(args, "integration", "direct"), "direct")
            return real_run(args)

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "pkg" / "mod.txt").write_text("ok\n", encoding="utf-8")

        exit_code, out = self.drive_direct(
            # Omit --integration entirely: argparse default must be direct.
            ["code", "--target", "pkg", "--base", "HEAD", "--task", "Edit"],
            repo_root=repo,
            plant=_plant,
            extra_patchers=[mock.patch.object(direct_mode, "run", _spy)],
        )
        self.assertEqual(exit_code, 0, out)
        self.assertTrue(called["direct"], "default code argv must dispatch to direct.run")
        env = json.loads(out)
        self.assertEqual(env["mode"], "direct")


class DirectDenyHelperUnitTests(ModeHarness):
    """Unit coverage for deny-glob matching (no full lifecycle)."""

    def test_path_matches_deny_patterns(self) -> None:
        from groklib.modes.direct_finalize import path_matches_deny

        self.assertTrue(path_matches_deny(".env"))
        self.assertTrue(path_matches_deny(".env.local"))
        self.assertTrue(path_matches_deny(".git/config"))
        self.assertTrue(path_matches_deny(".git/hooks/pre-commit"))
        self.assertTrue(path_matches_deny("keys/id_rsa.key"))
        self.assertTrue(path_matches_deny("pkg/server.pem"))
        self.assertTrue(path_matches_deny("bundle.p12"))
        self.assertTrue(path_matches_deny(".githooks/pre-push"))
        self.assertFalse(path_matches_deny("pkg/mod.txt"))
        self.assertFalse(path_matches_deny("README.md"))


class DirectProtectedPathRollbackTests(DirectModeHarness):
    """Disk-state guarantees: protected writes are rolled back, not only detected.

    Detection-only guards assert the error class while leaving the write on disk.
    These tests read the file after the run and require pre-run identity (or
    removal for created protected files).
    """

    def test_env_write_rolled_back_to_pre_run_bytes(self) -> None:
        """Pre-existing .env must be byte-identical after a protected-path-write fail."""
        repo = self.make_code_repo()
        original = b"DEBUG=false\nAPI_KEY=pre-run-value\n"
        env_path = repo / ".env"
        env_path.write_bytes(original)

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            # Simulate Grok appending to the operator .env (the live-test gap).
            with (repo_root / ".env").open("ab") as handle:
                handle.write(b"DEBUG=true\n")

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["error"]["class"], "protected-path-write")
        # THE missing assertion: disk state, not just error class.
        self.assertEqual(
            env_path.read_bytes(),
            original,
            "protected .env must be restored to pre-run bytes after direct run",
        )
        detail = env["error"].get("detail") or {}
        restored = detail.get("restored") or []
        self.assertIn(".env", restored)

    def test_created_protected_pem_is_removed(self) -> None:
        """A protected file Grok creates must be deleted on protected-path-write."""
        repo = self.make_code_repo()
        pem_path = repo / "leaked.pem"
        self.assertFalse(pem_path.exists())

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / "leaked.pem").write_text(
                "-----BEGIN FAKE-----\nsecret\n", encoding="utf-8"
            )
            # Legitimate non-protected edit in the same run must survive restore.
            (repo_root / "pkg" / "mod.txt").write_text(
                "module still edited\n", encoding="utf-8"
            )

        exit_code, out = self.drive_direct(self._direct_argv(), repo_root=repo, plant=_plant)
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "protected-path-write")
        self.assertFalse(
            pem_path.exists(),
            "Grok-created protected file must be removed after the run",
        )
        # Restore must not touch non-protected paths.
        self.assertEqual(
            (repo / "pkg" / "mod.txt").read_text(encoding="utf-8"),
            "module still edited\n",
        )

    def test_unsnapshottable_protected_change_fails_honestly(self) -> None:
        """Over-cap protected file: fail closed without claiming restore succeeded."""
        from groklib.modes import direct_protect

        repo = self.make_code_repo()
        # Small file that exceeds a patched tiny snapshot budget.
        original = b"X" * 64
        env_path = repo / ".env"
        env_path.write_bytes(original)

        def _plant(repo_root: pathlib.Path, _run_id: str) -> None:
            (repo_root / ".env").write_bytes(original + b"TAMPERED\n")

        with mock.patch.object(direct_protect, "DEFAULT_MAX_TOTAL_BYTES", 16):
            exit_code, out = self.drive_direct(
                self._direct_argv(), repo_root=repo, plant=_plant
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["error"]["class"], "protected-path-write")
        message = env["error"].get("message") or ""
        detail_blob = json.dumps(env["error"])
        self.assertIn("too large to roll back", message + detail_blob)
        # Must not falsely claim restore of the over-cap path.
        detail = env["error"].get("detail") or {}
        restored = detail.get("restored") or []
        unrestored = detail.get("unrestored") or []
        self.assertNotIn(".env", restored)
        self.assertIn(".env", unrestored)
        # File may still be tampered (honest: we could not roll it back).
        self.assertEqual(env_path.read_bytes(), original + b"TAMPERED\n")
