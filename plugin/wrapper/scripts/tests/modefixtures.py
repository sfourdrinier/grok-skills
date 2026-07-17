# wrapper/scripts/tests/modefixtures.py
#
# Shared harness for the review/reason mode tests (Task 10). It isolates
# XDG_STATE_HOME, points GROK_AGENT_BINARY at the fake grok CLI, and seeds a
# fake source ~/.grok with an auth.json. drive() runs grok_agent.main end to
# end while injecting, into every private home the mode creates, both the fake
# CLI control file (scenario + argv log path) AND a passing read-only
# ProfileApplied sandbox-events.jsonl (so sandbox.verify_enforcement, the
# security backstop every live mode runs, has real evidence to check). The
# private home is minted inside the mode, so the only clean seam is a thin
# wrapper around _shared.create_private_home that drops those two files after
# the real home is built (the same seam preflightfixtures uses).
#
# Temp-dir isolation is provided by TempHomeIsolationMixin: each test redirects
# tempfile.tempdir into its own "gsi-" directory so the global "gs-*" private-home
# scan sees only this test's homes (deterministic teardown/no-leak assertions).
# When ambient TMPDIR is already long (nested sandboxes), the mixin falls back
# to /tmp so leader-socket paths stay under the 100-byte AF_UNIX guard.

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from typing import List, Optional
from unittest import mock

import grok_agent
from groklib import authhome, sandbox
from groklib.modes import _shared

from tests.probedplatform import ProbedPlatformMixin
from tests.temphomeisolation import TempHomeIsolationMixin

_FAKE_BINARY = pathlib.Path(__file__).resolve().parent / "fake_grok.py"


def _passing_sandbox_event(profile: str, read_write_paths: Optional[List[str]] = None) -> dict:
    """A ProfileApplied event proving write-confinement was enforced (Task 0 shape).

    ``read_write_paths`` defaults to the empty list used by the read-only modes
    (review/reason), which have no writable roots to confine. Write-capable modes
    (code/verify) pass the concrete worktree + private-tmp grants so
    verify_enforcement's Grok r5 #3 "expected writable root is present" check passes.
    """
    return {
        "timestamp": "2026-07-14T19:30:00.000000Z",
        "event_type": "ProfileApplied",
        "profile": profile,
        "platform": "macos/seatbelt",
        "enforced": True,
        "restrict_network": False,
        "read_write_paths": list(read_write_paths) if read_write_paths is not None else [],
        "read_only_paths": ["/usr", "/opt"],
    }


class ModeHarness(ProbedPlatformMixin, TempHomeIsolationMixin, unittest.TestCase):
    """Fully-isolated environment for driving grok_agent.main(["review"|"reason", ...])."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-mode-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)

        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"XDG_STATE_HOME": self.state_home, "GROK_AGENT_BINARY": str(_FAKE_BINARY)},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        self.source_grok = pathlib.Path(self.tmp_root) / "grok-home" / ".grok"
        self.source_grok.mkdir(parents=True)
        (self.source_grok / "auth.json").write_text("{}\n", encoding="utf-8")

        self.argv_log_path = pathlib.Path(self.tmp_root) / "argv.log"

    def temp_home_prefix_dirs(self) -> set:
        """The set of private-home directories currently under this test's isolated $TMPDIR."""
        from groklib.runstate import TEMP_HOME_PREFIX

        temp_root = pathlib.Path(tempfile.gettempdir())
        return {str(entry) for entry in temp_root.glob(TEMP_HOME_PREFIX + "*") if entry.is_dir()}

    def drive(
        self,
        argv: List[str],
        scenario: str = "ok-json",
        repo_root: Optional[pathlib.Path] = None,
        sandbox_profile: Optional[str] = None,
    ):
        """Run main(argv), injecting the fake control + passing sandbox events into each home.

        When ``sandbox_profile`` is not overridden, the injected ProfileApplied
        event uses the DISTINCT custom profile name policy_for_mode now resolves
        (``grok-skills-<mode>``, Grok dogfood-2 #6), so verify_enforcement's
        profile-match check passes exactly as it does at runtime.
        """
        if sandbox_profile is None:
            sandbox_profile = sandbox.custom_profile_name(argv[0]) if argv else "read-only"
        real_create = authhome.create_private_home

        def _patched_create(**kwargs):
            home = real_create(**kwargs)
            (home.home_dir / "fake-grok-control.json").write_text(
                json.dumps({"scenario": scenario, "argvLog": str(self.argv_log_path)}),
                encoding="utf-8",
            )
            (home.grok_dir / "sandbox-events.jsonl").write_text(
                json.dumps(_passing_sandbox_event(sandbox_profile)) + "\n", encoding="utf-8"
            )
            return home

        patchers = [
            mock.patch.object(_shared, "create_private_home", _patched_create),
            mock.patch.object(_shared, "source_grok_dir", lambda: self.source_grok),
        ]

        buffer = io.StringIO()
        # Repo-agnostic wrapper: the repo root is derived from the resolved
        # --target (or cwd), so the harness runs main() with cwd set to the fixture
        # repo. A relative --target then resolves against it, and the real git
        # toplevel of the fixture repo IS repo_root -- exercising the production
        # path with no repo-root monkeypatch. cwd is always restored.
        original_cwd = os.getcwd()
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            if repo_root is not None:
                os.chdir(str(pathlib.Path(repo_root)))
            try:
                with contextlib.redirect_stdout(buffer):
                    exit_code = grok_agent.main(argv)
            finally:
                os.chdir(original_cwd)
        return exit_code, buffer.getvalue()

    def read_run_argv(self) -> List[str]:
        """Return the single grok-run argv the fake logged (version checks do not log)."""
        lines = [
            line for line in self.argv_log_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        records = [json.loads(line) for line in lines]
        self.assertEqual(len(records), 1, "expected exactly one grok-run argv log entry")
        return records[0]["argv"]

    def flag_value(self, argv: List[str], flag: str) -> Optional[str]:
        """Return the value token following ``flag`` in ``argv``, or None when absent."""
        for index, token in enumerate(argv):
            if token == flag and index + 1 < len(argv):
                return argv[index + 1]
        return None

    def read_prompt(self, argv: List[str]) -> str:
        """Read the prompt file the run built, from the argv --prompt-file value."""
        prompt_path = self.flag_value(argv, "--prompt-file")
        self.assertIsNotNone(prompt_path, "run argv must carry --prompt-file")
        return pathlib.Path(prompt_path).read_text(encoding="utf-8")


def make_review_repo(base: pathlib.Path) -> pathlib.Path:
    """Build a synthetic GIT repo with a shared-header AGENTS.md/CLAUDE.md root pair and a pkg/ target.

    Initialized as a real git repository (like production, where review's
    repository is always a ``git rev-parse --show-toplevel``): the review mode's
    filesystem defense-in-depth check now fails CLOSED if the repo change
    fingerprint cannot be captured (Grok dogfood-3 #7), so the fixture must be a
    genuine git checkout rather than a bare directory.
    """
    repo = pathlib.Path(base) / "repo"
    repo.mkdir(parents=True, exist_ok=False)
    header = "<!-- AGENTS.md | CLAUDE.md -->\n"
    body = "# Repository rules\n\nAlways read the rules before acting.\n"
    (repo / "AGENTS.md").write_text(header + body, encoding="utf-8")
    (repo / "CLAUDE.md").write_text(header + body, encoding="utf-8")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "module.txt").write_text("source under review\n", encoding="utf-8")

    def _git(args: List[str]) -> None:
        subprocess.run(
            ["git", "-C", str(repo)] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    _git(["init", "-q"])
    _git(["config", "user.name", "Grok CLI Test"])
    _git(["config", "user.email", "grok-cli-test@example.com"])
    _git(["config", "commit.gpgsign", "false"])
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "initial review tree"])
    return repo
