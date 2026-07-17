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


class PeerLifecycleTests(ProbedPlatformMixin, TempHomeIsolationMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
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

    def test_peer_start_writes_peer_json_and_running_envelope(self) -> None:
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

        running_holder: List[dict] = []
        stop_event = threading.Event()

        def _serve_once(session, running_env, preopened=None):
            running_holder.append(running_env)
            if preopened is not None:
                try:
                    preopened.close()
                except Exception:
                    pass
            # Write peer.json is already done; simulate prompt+stop quickly.
            stop_event.wait(timeout=2)
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
        self.assertEqual(running_holder[0]["status"], "running")
        peer_info = running_holder[0]["response"]["peer"]
        self.assertIn("sessionId", peer_info)
        self.assertIn("socketPath", peer_info)
        run_id = running_holder[0]["runId"]
        peer_path = runstate.state_root() / "runs" / run_id / "peer.json"
        self.assertTrue(peer_path.is_file())
        peer_doc = json.loads(peer_path.read_text(encoding="utf-8"))
        self.assertIn("wrapper", peer_doc)
        self.assertIn("child", peer_doc)
        self.assertIn("pid", peer_doc["wrapper"])
        self.assertIn("startToken", peer_doc["wrapper"])
        self.assertIn("pid", peer_doc["child"])
        self.assertIn("startToken", peer_doc["child"])
        stop_event.set()

    def test_start_parity_fails_closed_before_spawn(self) -> None:
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
            task=None,
            task_file=None,
        )
        spawn = mock.Mock(side_effect=AssertionError("must not spawn on parity failure"))
        with mock.patch.object(peer_mod, "_spawn_acp_child", spawn):
            with mock.patch.object(peer_mod, "require_probed_platform_for_live", return_value=None):
                with mock.patch.object(peer_mod, "check_version", return_value="0.0.0"):
                    with mock.patch.object(
                        peer_mod,
                        "_assert_start_parity",
                        side_effect=GrokWrapperError(
                            "sandbox-failure",
                            "start parity: missing sandbox profile capability",
                        ),
                    ):
                        with self.assertRaises(GrokWrapperError) as ctx:
                            peer_mod.run_peer_start(ns)
        self.assertEqual(ctx.exception.error_class, "sandbox-failure")
        spawn.assert_not_called()

    def test_peer_prompt_dead_child_is_acp_failure_with_reattach_hint(self) -> None:
        run_paths = runstate.create_run("peer-start")
        peer_doc = {
            "schemaVersion": 1,
            "lifecycle": "died",
            "sessionId": "s1",
            "socketPath": str(run_paths.run_dir / "peer.sock"),
            "wrapper": {"pid": 1, "startToken": "x"},
            "child": {"pid": 2, "startToken": "y"},
            "homePath": str(pathlib.Path(self.tmp_root) / "gone-home"),
            "worktreePath": str(self.repo),
            "leaseExpiresAt": time.time() + 3600,
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)
        ns = mock.Mock(run_id=run_paths.run_id, task="hi", task_file=None)
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod.run_peer_prompt(ns)
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        self.assertIn("reattach", str(ctx.exception).lower() + json.dumps(ctx.exception.detail).lower())

    def test_concurrent_prompt_rejected(self) -> None:
        # Serialization is enforced inside the resident control plane.
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = True
        session.run_id = "20260717T000000Z-aaaaaa"
        session.session_id = "s"
        session.acp = self.fake_acp
        session.progress = mock.Mock()
        session.progress.safe_emit = mock.Mock()
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod._handle_prompt(session, "second prompt")
        self.assertEqual(ctx.exception.error_class, "acp-failure")
        self.assertIn("in flight", str(ctx.exception).lower())

    def test_peer_stop_labels_confinement_and_destroys_home(self) -> None:
        source = pathlib.Path(self.tmp_root) / "grok" / ".grok"
        source.mkdir(parents=True)
        (source / "auth.json").write_text("{}\n", encoding="utf-8")

        home = self._fake_create_home()
        run_paths = runstate.create_run("peer-start")
        from groklib import worktree as worktree_mod

        wt = worktree_mod.create_external_worktree(
            repo_root=self.repo, base=self.base, run_id=run_paths.run_id
        )
        sentinel = ".grok-run-" + run_paths.run_id
        (wt.path / sentinel).write_text("", encoding="utf-8")
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
            "originalBaseline": None,
            "leaseExpiresAt": time.time() + 3600,
            "model": "grok-4.5",
        }
        runstate.write_json_atomic(run_paths.run_dir / "peer.json", peer_doc)
        # Plant a control socket that answers stop with a finalize request path.
        # Direct unit path: call finalize helper.
        from groklib.modes import peer_finalize

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
        stage.result = mock.Mock(answer="done", session_id="sess-1", request_id=None, stop_reason="end_turn", model_usage=None, turns=1, raw_usage=None)

        with mock.patch.object(peer_finalize, "destroy_private_home", self._fake_destroy_home):
            result = peer_finalize.finalize_peer_session(
                run_paths=run_paths,
                peer_doc=peer_doc,
                home_path=pathlib.Path(home.home_dir),
                worktree=wt,
                contract=None,
                original_baseline=None,
                stage=stage,
            )
        manifest_path = run_paths.run_dir / "implementation-handoff.json"
        self.assertTrue(manifest_path.is_file(), "finalize must write handoff artifacts")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("confinement"), "worktree-final-diff-only")
        self.assertFalse(pathlib.Path(home.home_dir).exists(), "private home must be destroyed")

    def test_reaper_skips_live_peer_home_reaps_dead(self) -> None:
        live_home = pathlib.Path(tempfile.mkdtemp(prefix=runstate.TEMP_HOME_PREFIX))
        dead_home = pathlib.Path(tempfile.mkdtemp(prefix=runstate.TEMP_HOME_PREFIX))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(live_home), True))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(dead_home), True))
        for h in (live_home, dead_home):
            os.chmod(str(h), 0o700)
            runstate.write_owner_marker(h, runstate.new_run_id())
            runstate.write_home_liveness_marker(h, os.getpid())

        # Live peer: child is this process, lease fresh.
        runstate.write_peer_lease(
            live_home,
            child_pid=os.getpid(),
            child_start_token=platformsupport.process_start_token(os.getpid()),
            lease_seconds=3600,
        )
        # Dead peer: a pid that has already exited (no live child).
        dead_proc = __import__("subprocess").Popen(["true"])
        dead_proc.wait()
        runstate.write_peer_lease(
            dead_home,
            child_pid=dead_proc.pid,
            child_start_token="dead-child-token",
            lease_seconds=3600,
        )
        # Age both past the live-start window.
        past = time.time() - (runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS + 100)
        os.utime(live_home, (past, past))
        os.utime(dead_home, (past, past))

        removed = runstate.audit_stale_temp_homes(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS)
        self.assertIn(str(dead_home), removed)
        self.assertNotIn(str(live_home), removed)
        self.assertTrue(live_home.exists())
        self.assertFalse(dead_home.exists())

    def test_session_update_chunk_redacted_in_progress_and_envelope(self) -> None:
        secret = _split_bearer_fixture()
        self.fake_acp.chunk_secret = secret
        run_paths = runstate.create_run("peer-start")
        progress = __import__("groklib.progress", fromlist=["ProgressWriter"]).ProgressWriter(
            run_paths.run_id, run_paths.progress_path
        )
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = False
        session.run_id = run_paths.run_id
        session.session_id = "s1"
        session.acp = self.fake_acp
        session.progress = progress
        session.run_paths = run_paths
        session.model = "grok-4.5"
        session.child = self.fake_child
        session.peer_doc = {"lifecycle": "running", "child": {"pid": os.getpid()}}
        session.renew_lease = mock.Mock()
        env = peer_mod._handle_prompt(session, "task")
        # progress.jsonl must not contain the contiguous secret
        progress_text = run_paths.progress_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, progress_text)
        # turn envelope result must be redacted
        dumped = json.dumps(env)
        self.assertNotIn(secret, dumped)
        self.assertEqual(env["status"], "success")

    def test_control_socket_mode_0600_foreign_refused(self) -> None:
        if not platformsupport.is_posix():
            self.skipTest("unix socket 0600 is POSIX-only")
        run_paths = runstate.create_run("peer-start")
        # Production puts the control socket under the short private-home path,
        # not run_dir, because a nested XDG_STATE_HOME run_dir blows the AF_UNIX
        # ~104-byte limit. Mirror that here with a short temp path so the test
        # exercises the 0600 + foreign-uid logic, not the length guard.
        sock_dir = pathlib.Path(tempfile.mkdtemp(prefix="gp-"))
        self.addCleanup(shutil.rmtree, str(sock_dir), ignore_errors=True)
        sock_path = sock_dir / "p.sock"
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session.run_id = run_paths.run_id
        session.run_paths = run_paths
        session.socket_path = sock_path
        session.session_id = "s"
        session._stop_requested = False
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = False
        session.acp = self.fake_acp
        session.progress = mock.Mock()
        session.progress.safe_emit = mock.Mock()
        session.model = "grok-4.5"
        session.renew_lease = mock.Mock()
        session.peer_doc = {}
        session.home = None
        session.worktree = None
        session.contract = None
        session.original_baseline = None
        session.child = self.fake_child

        ready = threading.Event()
        final_holder: List[dict] = []

        def _server():
            running = envelope_mod.build_envelope(
                run_id=run_paths.run_id, mode="peer-start", status="running",
                response={"peer": {"sessionId": "s", "socketPath": str(sock_path)}},
            )
            # serve one connection then stop
            srv = peer_mod._open_control_socket(sock_path)
            ready.set()
            try:
                conn, _addr = srv.accept()
                with conn:
                    # Foreign uid check is inside handler; same-uid succeeds.
                    peer_mod._handle_control_connection(session, conn)
            finally:
                srv.close()
            final_holder.append(running)

        t = threading.Thread(target=_server, daemon=True)
        t.start()
        self.assertTrue(ready.wait(2))
        mode = stat.S_IMODE(os.stat(str(sock_path)).st_mode)
        self.assertEqual(mode, 0o600)
        # Same-uid prompt should be accepted.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        client.sendall(json.dumps({"op": "prompt", "task": "hi"}).encode("utf-8") + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        client.close()
        t.join(timeout=5)
        self.assertTrue(data.strip())
        # Foreign connection refusal unit: call gate with mismatched uid.
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod._assert_peer_uid(os.getuid() + 1)
        self.assertEqual(ctx.exception.error_class, "acp-failure")


if __name__ == "__main__":
    unittest.main()
