# wrapper/scripts/tests/peer_test_base.py
#
# Shared fakes + setUp + helpers for the peer-channel tests
# (split for the 900-line cap; test_mode_peer and test_mode_peer_finalize
# both subclass PeerTestBase).

# wrapper/scripts/tests/test_mode_peer.py
#
# Experimental ACP peer channel lifecycle (Task 5.3). Fakes the ACP client and
# child process; exercises start parity, peer.json identities, prompt
# serialization, stop finalize, redaction, control socket 0600, and reaper.

from __future__ import annotations

import json
import os
import pathlib
import socket
import stat
import shutil
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from groklib import GrokWrapperError
from groklib import envelope as envelope_mod
from groklib import platformsupport
from groklib import runstate
from groklib.modes import peer as peer_mod

from tests.temphomeisolation import TempHomeIsolationMixin
from tests.probedplatform import ProbedPlatformMixin
from tests import gitfixtures


def _split_bearer_fixture() -> str:
    # Repo rule 8: never hold a contiguous secret-shaped literal in fixtures.
    return "Bear" + "er eyJhbGciOi" + "JIUzI1NiJ9." + "aaa.bbb"


class _FakeAcpClient:
    """In-process stand-in for groklib.acp.AcpClient."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.session_id = "sess-test-1"
        self.prompt_calls = 0
        self.cancelled = False
        self.closed = False
        self._in_flight = False
        self.chunk_secret = None  # set by tests that need redaction

    def initialize(self) -> dict:
        return {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "_meta": {
                "x.ai/hooks": {
                    "blockingEvents": ["pre_tool_use"],
                    "decisions": ["deny"],
                }
            },
        }

    def session_new(self, cwd: str, mcp_servers: Optional[list] = None, **kwargs: Any) -> dict:
        assert mcp_servers == [] or mcp_servers is None or mcp_servers == []
        return {"sessionId": self.session_id}

    def session_prompt(
        self,
        session_id: str,
        text: str,
        on_update=None,
        **kwargs: Any,
    ) -> dict:
        if self._in_flight:
            raise GrokWrapperError(
                "acp-failure",
                "a prompt is already in flight for this peer session",
            )
        self._in_flight = True
        try:
            self.prompt_calls += 1
            if self.chunk_secret and on_update:
                on_update(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": self.chunk_secret},
                            }
                        },
                    }
                )
            elif on_update:
                on_update(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "ok"},
                            }
                        },
                    }
                )
            return {"stopReason": "end_turn", "usage": {}}
        finally:
            self._in_flight = False

    def session_cancel(self, session_id: str, **kwargs: Any) -> dict:
        self.cancelled = True
        return {}

    def close(self) -> None:
        self.closed = True


class _FakeChild:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode = None
        self.stdin = mock.Mock()
        self.stdout = mock.Mock()
        self.stderr = mock.Mock()

    def poll(self) -> Optional[int]:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class PeerTestBase(ProbedPlatformMixin, TempHomeIsolationMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # peer-start arms a module-global stdout suppress; a faked serve path may
        # skip the clear, leaking into later in-process tests. Always clear it.
        from groklib.envelope import clear_peer_resident_stdout_suppress
        self.addCleanup(clear_peer_resident_stdout_suppress)
        self.tmp_root = tempfile.mkdtemp(prefix="grok-peer-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp_root, True))
        self.state_home = os.path.join(self.tmp_root, "state")
        os.makedirs(self.state_home, exist_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": self.state_home,
                "GROK_EXPERIMENTAL_ACP": "1",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

        parent = tempfile.mkdtemp(prefix="peer-repo-", dir=self.tmp_root)
        self.repo = gitfixtures.make_repo(parent)
        (self.repo / "pkg").mkdir(exist_ok=True)
        (self.repo / "pkg" / "a.txt").write_text("x\n", encoding="utf-8")
        subprocess = __import__("subprocess")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "pkg/a.txt"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "pkg"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base = gitfixtures.head_revision(self.repo)
        self.fake_acp = _FakeAcpClient()
        self.fake_child = _FakeChild()

    def _patch_spawn_and_acp(self):
        return mock.patch.multiple(
            peer_mod,
            _spawn_acp_child=mock.Mock(return_value=self.fake_child),
            AcpClient=mock.Mock(return_value=self.fake_acp),
            create_private_home=self._fake_create_home,
            destroy_private_home=self._fake_destroy_home,
            source_grok_dir=lambda: pathlib.Path(self.tmp_root) / "grok" / ".grok",
        )

    def _fake_create_home(self, **kwargs):
        from groklib.authhome import PrivateHome

        home_dir = pathlib.Path(tempfile.mkdtemp(prefix=runstate.TEMP_HOME_PREFIX))
        grok_dir = home_dir / ".grok"
        grok_dir.mkdir()
        runstate.write_owner_marker(home_dir, runstate.new_run_id())
        runstate.write_home_liveness_marker(home_dir, os.getpid())
        (grok_dir / "config.toml").write_text("# peer test\n", encoding="utf-8")
        return PrivateHome(home_dir=home_dir, grok_dir=grok_dir, config_path=grok_dir / "config.toml")

    def _fake_destroy_home(self, home):
        import shutil

        if home.home_dir.exists():
            shutil.rmtree(str(home.home_dir), ignore_errors=True)
        return {"status": "clean", "detail": None}

    def _peer_finalize_fixture(self, *, plant_sandbox: bool = False):
        """Shared worktree + home + peer_doc for finalize unit tests."""
        from groklib import worktree as worktree_mod
        from groklib import worktree_escape
        from tests.modefixtures import _passing_sandbox_event
        from groklib.sandbox import custom_profile_name

        home = self._fake_create_home()
        run_paths = runstate.create_run("peer-start")
        # Advance lifecycle so terminalize can complete.
        rec = runstate.load_run_record(run_paths.run_id)
        rev = int(rec.get("recordRevision", 0))
        if rec.get("lifecycle") == "created":
            rec = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(rec["recordRevision"])
        wt = worktree_mod.create_external_worktree(
            repo_root=self.repo, base=self.base, run_id=run_paths.run_id
        )
        runstate.cas_update_run_record(
            run_paths,
            rev,
            {
                "worktreePath": str(wt.path),
                "worktreeBranch": wt.branch,
                "baseRevision": wt.base_revision,
                "repository": str(self.repo),
                "status": "running",
            },
        )
        sentinel = ".grok-run-" + run_paths.run_id
        (wt.path / sentinel).write_text("", encoding="utf-8")
        private_tmp = home.home_dir / "tmp"
        private_tmp.mkdir(exist_ok=True)
        if plant_sandbox:
            grants = [str(wt.path.resolve()), str(private_tmp.resolve())]
            (home.grok_dir / "sandbox-events.jsonl").write_text(
                json.dumps(
                    _passing_sandbox_event(
                        custom_profile_name("peer"), read_write_paths=grants
                    )
                )
                + "\n",
                encoding="utf-8",
            )
        baseline = worktree_escape.capture_original_checkout_baseline(self.repo)
        sock_path = run_paths.run_dir / "peer.sock"
        peer_doc = {
            "schemaVersion": 1,
            "lifecycle": "running",
            "sessionId": "sess-1",
            "socketPath": str(sock_path),
            "wrapper": {
                "pid": os.getpid(),
                "startToken": platformsupport.process_start_token(os.getpid()),
            },
            "child": {
                "pid": os.getpid(),
                "startToken": platformsupport.process_start_token(os.getpid()),
            },
            "homePath": str(home.home_dir),
            "worktreePath": str(wt.path),
            "worktreeBranch": wt.branch,
            "baseRevision": wt.base_revision,
            "repoRoot": str(self.repo),
            "targetRelative": "pkg",
            "sentinelName": sentinel,
            "contract": None,
            "originalBaseline": dict(baseline),
            "leaseExpiresAt": time.time() + 3600,
            "model": "grok-4.5",
            # Fixture plants the sentinel (above), i.e. Grok created it after a
            # prompt; promptsHandled>0 so peer-stop enforces it (require_sentinel).
            "promptsHandled": 1,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)

        stage_acc = mock.Mock()
        stage_acc.commands = []
        stage_acc.changed_files = []
        stage_acc.diff_summary = None
        stage_acc.effective_working_directory = str(wt.path)
        stage_acc.warnings = []
        stage_acc.verifier = None
        stage = mock.Mock()
        stage.worktree = wt
        stage.run_id = run_paths.run_id
        stage.acc = stage_acc
        stage.progress = mock.Mock()
        stage.progress.safe_emit = mock.Mock()
        stage.result = mock.Mock(
            answer="done",
            session_id="sess-1",
            request_id=None,
            stop_reason="end_turn",
            model_usage=None,
            turns=1,
            raw_usage=None,
        )
        return home, run_paths, wt, peer_doc, stage, baseline

    def _plant_worktree_change(self, wt, *, path: str = "pkg/mod.txt", text: str = "peer-change\n") -> None:
        target = wt.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
