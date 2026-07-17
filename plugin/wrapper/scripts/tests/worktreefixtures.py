# wrapper/scripts/tests/worktreefixtures.py
#
# Shared harness for the code/verify worktree-mode tests (Task 11). It extends
# the review/reason ModeHarness (isolated XDG_STATE_HOME, fake grok binary,
# seeded source ~/.grok, argv log) with the pieces the worktree modes need:
#
#   * a fake `pnpm` executable the code build gate + install shell out to, whose
#     install always exits 0 and whose gate scripts exit ${FAKE_PNPM_GATE_EXIT},
#   * a real git fixture repo whose pkg/ target carries a committed package.json,
#   * a real registered external worktree for verify to adopt,
#   * a `drive` that, at the private-home creation seam (which fires AFTER the
#     worktree exists), injects the fake control file + a passing workspace
#     sandbox-events.jsonl and lets each test PLANT the files Grok would have
#     written (the cwd sentinel, an artifact dir, a source edit) into the
#     worktree or the original checkout.
#
# The private homes are minted under the REAL $TMPDIR (like modefixtures) so the
# true macOS leader-socket path length is exercised.

import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional
from unittest import mock

import grok_agent
from groklib import runstate
from groklib import sandbox as sandbox_mod
from groklib import worktree as worktree_mod
from groklib.modes import _worktree

from tests import gitfixtures
from tests.modefixtures import ModeHarness, _passing_sandbox_event

# install always succeeds; gate scripts (invoked as `--filter <name> <script>`)
# exit the FAKE_PNPM_GATE_EXIT code (default 0), so a test can fail exactly the
# build gate without also failing the earlier install.
_FAKE_PNPM_SCRIPT = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    "  install) exit 0 ;;\n"
    "  *) exit ${FAKE_PNPM_GATE_EXIT:-0} ;;\n"
    "esac\n"
)


class WorktreeModeHarness(ModeHarness):
    """Drives grok_agent.main(["code"|"verify", ...]) against real git + a fake pnpm, with worktree planting."""

    def setUp(self) -> None:
        super().setUp()
        self.pnpm_binary = pathlib.Path(self.tmp_root) / "fake_pnpm.sh"
        self.pnpm_binary.write_text(_FAKE_PNPM_SCRIPT, encoding="utf-8")
        os.chmod(str(self.pnpm_binary), 0o755)

    def _git(self, repo: pathlib.Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def make_code_repo(self, *, build_script: bool = True) -> pathlib.Path:
        """A real git repo whose committed pkg/ target carries a package.json (build or typecheck+lint).

        A ``pnpm-lock.yaml`` is committed at the repo root so ProjectConfig detects
        pnpm as the build-gate package manager (matching the fake pnpm the harness
        injects via GROK_PACKAGE_MANAGER_BINARY).
        """
        parent = tempfile.mkdtemp(prefix="grok-cli-code-repo-", dir=self.tmp_root)
        repo = gitfixtures.make_repo(parent)
        scripts = {"build": "true"} if build_script else {"typecheck": "true", "lint": "true"}
        manifest = {"name": "pkg-under-test", "scripts": scripts}
        (repo / "pkg" / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        self._git(repo, "add", "pkg/package.json", "pnpm-lock.yaml")
        self._git(repo, "commit", "-q", "-m", "add pkg package.json + pnpm lockfile")
        return repo

    def make_registered_worktree(self, repo_root: pathlib.Path) -> worktree_mod.ExternalWorktree:
        """Create a real registered external worktree (as a prior code run would) for verify to adopt."""
        base = gitfixtures.head_revision(repo_root)
        return worktree_mod.create_external_worktree(
            repo_root=repo_root, base=base, run_id=runstate.new_run_id()
        )

    def _worktree_dirs(self) -> set:
        """The set of external worktree directories currently under the isolated state root."""
        base = runstate.state_root() / "worktrees"
        return {
            str(child)
            for slug in base.glob("*")
            if slug.is_dir()
            for child in slug.glob("*")
            if child.is_dir()
        }

    def _new_worktree(self, preexisting: set) -> pathlib.Path:
        """Return the single worktree created since ``preexisting`` was captured.

        Run ids embed only a whole-second timestamp plus a random suffix, so a
        test that drives two runs in the same second (e.g. the build-gate test,
        whose first run RETAINS its worktree) cannot pick the current run's
        worktree by lexical name -- the random suffix decides the max and the
        stale retained worktree wins about half the time. Diffing against the
        pre-drive snapshot identifies exactly the worktree this run created.
        """
        created = sorted(self._worktree_dirs() - preexisting)
        if not created:
            raise AssertionError("no new external worktree was created for this run")
        if len(created) != 1:
            raise AssertionError("expected exactly one new worktree, found {}".format(created))
        return pathlib.Path(created[0])

    def drive(
        self,
        argv: List[str],
        *,
        repo_root: pathlib.Path,
        scenario: str = "ok-json",
        control_extra: Optional[Dict[str, str]] = None,
        sandbox_profile: Optional[str] = None,
        plant: Optional[Callable[[pathlib.Path, str], None]] = None,
        env_extra: Optional[Dict[str, str]] = None,
        extra_patchers: Optional[List] = None,
    ):
        """Run main(argv), injecting control + sandbox evidence and letting the test plant worktree files.

        ``plant(worktree_path, run_id)`` fires at the private-home creation seam,
        which is AFTER the worktree exists but BEFORE Grok runs -- exactly where a
        test simulates the files Grok would have written (cwd sentinel, artifact
        dir, source edit) inside the worktree or the original checkout.
        """
        # Worktree-mode tests still exercise the isolated-worktree path. When the
        # suite omits --integration, inject worktree so the new direct default
        # does not re-route every pre-existing code test onto hardened-direct.
        argv = list(argv)
        if (
            argv
            and argv[0] == "code"
            and "--integration" not in argv
            and "--continue-run" not in argv
        ):
            argv = [argv[0], "--integration", "worktree"] + argv[1:]

        # Match the DISTINCT custom profile policy_for_mode now resolves
        # (grok-skills-<mode>, Grok dogfood-2 #6) so verify_enforcement passes.
        if sandbox_profile is None:
            sandbox_profile = sandbox_mod.custom_profile_name(argv[0]) if argv else "workspace"
        real_create = _worktree.create_private_home
        preexisting_worktrees = self._worktree_dirs()
        # verify adopts an existing worktree (passed via --worktree) and creates
        # none; code creates a fresh one; continue-run reuses a retained worktree.
        # The plant target is the adopted/continued path when set, else the worktree
        # this code run just created. plant(run_id) must be the NEW run id (sentinel).
        adopted_worktree = self.flag_value(argv, "--worktree")
        continue_run_id = self.flag_value(argv, "--continue-run")

        def _plant_run_id(worktree_path: pathlib.Path) -> str:
            """Resolve the run id whose sentinel plant must create.

            Fresh code runs name the worktree after the run id. Continuation reuses
            the prior worktree path, so the path name is the prior id; plant must
            use the NEW run id (the only non-prior run currently in 'running').
            """
            if continue_run_id is None:
                return worktree_path.name
            runs_root = runstate.state_root() / "runs"
            for child in sorted(runs_root.iterdir(), reverse=True):
                if not child.is_dir() or child.name == continue_run_id:
                    continue
                try:
                    rec = runstate.load_run_record(child.name)
                except Exception:
                    continue
                if rec.get("mode") == "code" and rec.get("lifecycle") in (
                    "running",
                    "created",
                    "finalizing",
                ):
                    return child.name
            raise AssertionError(
                "could not resolve new run id for --continue-run plant (prior={})".format(
                    continue_run_id
                )
            )

        def _patched_create(**kwargs):
            home = real_create(**kwargs)
            control: Dict[str, str] = {"scenario": scenario, "argvLog": str(self.argv_log_path)}
            if control_extra:
                control.update(control_extra)
            (home.home_dir / "fake-grok-control.json").write_text(
                json.dumps(control), encoding="utf-8"
            )
            # code/verify are write-capable, so the ProfileApplied evidence must grant
            # write to the run's legitimate writable roots (the external worktree and
            # its private tmp) for verify_enforcement's Grok r5 #3 presence check. The
            # worktree exists at this seam (prepare created/adopted it before the home).
            if adopted_worktree is not None:
                worktree_path = pathlib.Path(adopted_worktree)
            elif continue_run_id is not None:
                prior = runstate.load_run_record(continue_run_id)
                worktree_path = pathlib.Path(prior["worktreePath"])
            else:
                worktree_path = self._new_worktree(preexisting_worktrees)
            private_tmp = home.home_dir / "tmp"
            grants = [str(worktree_path.resolve()), str(private_tmp.resolve())]
            (home.grok_dir / "sandbox-events.jsonl").write_text(
                json.dumps(_passing_sandbox_event(sandbox_profile, read_write_paths=grants)) + "\n",
                encoding="utf-8",
            )
            if plant is not None:
                plant(worktree_path, _plant_run_id(worktree_path))
            return home

        patchers = [
            mock.patch.object(_worktree, "create_private_home", _patched_create),
            mock.patch.object(_worktree, "source_grok_dir", lambda: self.source_grok),
        ]
        if extra_patchers:
            patchers.extend(extra_patchers)

        buffer = io.StringIO()
        # Repo-agnostic wrapper: run main() with cwd set to the fixture repo so the
        # repo root is derived from the resolved --target's real git toplevel (no
        # repo-root monkeypatch). cwd is always restored.
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
