# wrapper/scripts/tests/test_mode_peer.py
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
from typing import List
from unittest import mock

import shutil
from groklib import GrokWrapperError
from groklib import envelope as envelope_mod
from groklib import grokcli
from groklib import platformsupport
from groklib import runstate
from groklib.modes import code as code_mode
from groklib.modes import peer as peer_mod
from groklib.modes._envelope import _policy_field

from tests import gitfixtures
from tests.peer_test_base import PeerTestBase, _FakeAcpClient, _FakeChild, _split_bearer_fixture


class PeerLifecycleTests(PeerTestBase):
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
        # Fake home creation + source so a clean CI home (no ~/.grok/auth.json)
        # does not raise auth-missing in the REAL create_private_home before the
        # mocked _assert_start_parity is reached (CI-breaker).
        with mock.patch.object(peer_mod, "source_grok_dir", lambda: source), \
             mock.patch.object(peer_mod, "create_private_home", self._fake_create_home), \
             mock.patch.object(peer_mod, "_spawn_acp_child", spawn):
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
        # A post-load peer-prompt failure returns a failure envelope tagged with
        # the REQUESTED run id (not a raise under a fresh id), so callers keep
        # correlation for stop/cleanup.
        env = peer_mod.run_peer_prompt(ns)
        self.assertEqual(env["status"], "failure")
        self.assertEqual(env["mode"], "peer-prompt")
        self.assertEqual(env["runId"], run_paths.run_id)
        self.assertEqual(env["error"]["class"], "acp-failure")
        self.assertIn("reattach", json.dumps(env).lower())

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

    def test_peer_start_single_stdout_envelope(self) -> None:
        """Finding 7: resident emits only the running envelope on stdout."""
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
        stdout_writes: List[str] = []
        real_emit = envelope_mod.emit_envelope

        def _spy_emit(env, path):
            # Capture only actual stdout prints (not suppressed).
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                real_emit(env, path)
            text = buf.getvalue()
            if text.strip():
                stdout_writes.append(text)

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
                        with mock.patch.object(peer_mod, "emit_envelope", side_effect=_spy_emit):
                            with mock.patch.object(envelope_mod, "emit_envelope", side_effect=_spy_emit):
                                final = peer_mod.run_peer_start(ns)
                                # Simulate entrypoint post-return emit (must be suppressed).
                                envelope_mod.emit_envelope(final, None)
        self.assertEqual(len(stdout_writes), 1, stdout_writes)
        first = json.loads(stdout_writes[0])
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["mode"], "peer-start")

    def test_control_socket_payload_secret_scanned(self) -> None:
        """Finding 8: socket payloads route through assert_no_secret_material."""
        from groklib.modes import peer_control

        conn = mock.Mock()
        with mock.patch.object(peer_control, "assert_no_secret_material") as scan:
            peer_control.write_json_line(conn, {"type": "result", "envelope": {"ok": True}})
            scan.assert_called()
            conn.sendall.assert_called_once()
            # Residual secret-shaped key that redaction renames still scanned after redact.
            scan.reset_mock()
            # Force the post-redact scan to reject so the fail-closed path is covered.
            scan.side_effect = envelope_mod.SecretMaterialError("secret at $.x")
            with self.assertRaises(envelope_mod.SecretMaterialError):
                peer_control.write_json_line(conn, {"type": "result", "envelope": {"ok": True}})

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
        # Dead peer: the OWNER (resident wrapper) is dead. A live owner is never
        # reaped regardless of lease staleness (review [34]), so a genuinely
        # reapable home must have a dead owner, not just a dead child/stale lease.
        dead_proc = __import__("subprocess").Popen(["true"])
        dead_proc.wait()
        runstate.write_home_liveness_marker(dead_home, dead_proc.pid)
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

    def test_reaper_keeps_live_owner_despite_stale_lease(self) -> None:
        # [34]: an idle peer whose lease expired but whose resident wrapper
        # (owner) is still alive must NOT be reaped - the lease is a keepalive
        # hint, not evidence of death.
        home = pathlib.Path(tempfile.mkdtemp(prefix=runstate.TEMP_HOME_PREFIX))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(home), True))
        os.chmod(str(home), 0o700)
        runstate.write_owner_marker(home, runstate.new_run_id())
        runstate.write_home_liveness_marker(home, os.getpid())  # owner alive
        runstate.write_peer_lease(
            home,
            child_pid=os.getpid(),
            child_start_token=platformsupport.process_start_token(os.getpid()),
            lease_seconds=-1,  # already expired -> stale
        )
        past = time.time() - (runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS + 100)
        os.utime(home, (past, past))
        removed = runstate.audit_stale_temp_homes(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS)
        self.assertNotIn(str(home), removed)
        self.assertTrue(home.exists())

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
        session.sentinel_name = ".grok-run-test"
        session.worktree = mock.Mock(path=self.repo)
        session.contract = None
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

    def test_first_prompt_prepends_cwd_sentinel_directive(self) -> None:
        # B5: the wrapper no longer plants the sentinel; Grok creates it as its
        # mandatory first action, driven by a directive prepended to the FIRST
        # prompt only. This makes the stop-time proof genuine.
        from groklib.progress import ProgressWriter

        run_paths = runstate.create_run("peer-start")
        progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = False
        session.sentinel_name = ".grok-run-" + run_paths.run_id
        session.worktree = mock.Mock(path=self.repo)
        session.contract = None
        session.run_id = run_paths.run_id
        session.session_id = "s"
        session.progress = progress
        session.run_paths = run_paths
        session.model = "grok-4.5"
        session.child = self.fake_child
        session.peer_doc = {"lifecycle": "running", "child": {"pid": os.getpid()}}
        session.renew_lease = mock.Mock()
        sent = []
        session.acp = mock.Mock()
        session.acp.session_prompt = mock.Mock(
            side_effect=lambda session_id, text, on_update: sent.append(text)
            or {"stopReason": "end_turn", "usage": {}}
        )
        peer_mod._handle_prompt(session, "do the task")
        peer_mod._handle_prompt(session, "second turn")
        self.assertIn(session.sentinel_name, sent[0])
        self.assertIn("do the task", sent[0])  # task carried through the payload
        self.assertEqual(sent[1], "second turn")  # no directive/rules on later turns
        self.assertEqual(session.peer_doc["promptsHandled"], 2)

    def test_first_prompt_fails_closed_on_unreadable_rules(self) -> None:
        # A non-UTF-8 CLAUDE.md must fail the first peer prompt CLOSED
        # (rules-parity-failure), not run Grok with zero governing rules.
        from groklib.progress import ProgressWriter

        wt = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(wt), ignore_errors=True)
        (wt / "AGENTS.md").write_text("project rules\n", encoding="utf-8")
        (wt / "CLAUDE.md").write_bytes(b"\xff\xfe not valid utf-8\n")
        run_paths = runstate.create_run("peer-start")
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = False
        session.sentinel_name = ".grok-run-" + run_paths.run_id
        session.worktree = mock.Mock(path=wt)
        session.contract = None
        session.run_id = run_paths.run_id
        session.session_id = "s"
        session.progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        session.run_paths = run_paths
        session.model = "grok-4.5"
        session.child = self.fake_child
        session.peer_doc = {"lifecycle": "running", "child": {"pid": os.getpid()}}
        session.renew_lease = mock.Mock()
        session.acp = mock.Mock()  # must not be reached (discovery raises first)
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod._handle_prompt(session, "task")
        self.assertEqual(ctx.exception.error_class, "rules-parity-failure")
        session.acp.session_prompt.assert_not_called()

    def test_first_prompt_discovers_rules_from_operator_repo_not_worktree(self) -> None:
        # Rules/parity are read from the OPERATOR checkout (repoRoot), which sees
        # uncommitted rule files, NOT the committed-base worktree. A bad CLAUDE.md
        # in repoRoot + a CLEAN worktree must fail the prompt closed (proving the
        # discovery source is repoRoot, not the worktree).
        from groklib.progress import ProgressWriter

        repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(repo), ignore_errors=True)
        (repo / "CLAUDE.md").write_bytes(b"\xff\xfe not valid utf-8\n")
        clean_wt = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(clean_wt), ignore_errors=True)
        run_paths = runstate.create_run("peer-start")
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = False
        session.sentinel_name = ".grok-run-" + run_paths.run_id
        session.worktree = mock.Mock(path=clean_wt)
        session.contract = None
        session.run_id = run_paths.run_id
        session.session_id = "s"
        session.progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        session.run_paths = run_paths
        session.model = "grok-4.5"
        session.child = self.fake_child
        session.peer_doc = {
            "lifecycle": "running",
            "child": {"pid": os.getpid()},
            "repoRoot": str(repo),
        }
        session.renew_lease = mock.Mock()
        session.acp = mock.Mock()  # must not be reached (discovery raises first)
        with self.assertRaises(GrokWrapperError) as ctx:
            peer_mod._handle_prompt(session, "task")
        self.assertEqual(ctx.exception.error_class, "rules-parity-failure")
        session.acp.session_prompt.assert_not_called()

    def test_prompt_attempt_persisted_before_acp_wait(self) -> None:
        # If session_prompt errors AFTER Grok ran tools, promptsHandled must
        # already be 1 (persisted) so peer-stop still enforces the cwd sentinel
        # instead of finalizing a mutated worktree unverified.
        from groklib.progress import ProgressWriter

        wt = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(wt), ignore_errors=True)
        run_paths = runstate.create_run("peer-start")
        session = peer_mod.PeerSession.__new__(peer_mod.PeerSession)
        session._prompt_lock = threading.Lock()
        session._prompt_in_flight = False
        session.sentinel_name = ".grok-run-" + run_paths.run_id
        session.worktree = mock.Mock(path=wt)
        session.contract = None
        session.run_id = run_paths.run_id
        session.session_id = "s"
        session.progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        session.run_paths = run_paths
        session.model = "grok-4.5"
        session.child = self.fake_child
        session.peer_doc = {
            "lifecycle": "running",
            "child": {"pid": os.getpid()},
            "repoRoot": str(wt),
        }
        # Record promptsHandled at each renew_lease (persist) call.
        persisted = []
        session.renew_lease = mock.Mock(
            side_effect=lambda: persisted.append(int(session.peer_doc.get("promptsHandled", 0)))
        )
        session.acp = mock.Mock()
        session.acp.session_prompt = mock.Mock(
            side_effect=RuntimeError("ACP error after Grok ran tools")
        )
        with self.assertRaises(RuntimeError):
            peer_mod._handle_prompt(session, "task")
        # The attempt was counted AND persisted BEFORE the failing prompt.
        self.assertEqual(session.peer_doc.get("promptsHandled"), 1)
        self.assertIn(1, persisted)

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
        session.sentinel_name = ".grok-run-test"
        session.worktree = mock.Mock(path=self.repo)
        session.contract = None
        session.acp = self.fake_acp
        session.progress = mock.Mock()
        session.progress.safe_emit = mock.Mock()
        session.model = "grok-4.5"
        session.renew_lease = mock.Mock()
        session.peer_doc = {}
        session.home = None
        session.worktree = mock.Mock(path=self.repo)
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


class PeerStartPolicyParityTests(PeerTestBase):
    """peer-start envelope policy and spawn kwargs must share one effective config."""

    def test_peer_start_envelope_policy_matches_code_tools_and_shared_helper(self) -> None:
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
        spawn_kwargs_holder: List[dict] = []

        def _serve_once(session, running_env, preopened=None):
            running_holder.append(running_env)
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

        def _capture_spawn(**kwargs):
            spawn_kwargs_holder.append(kwargs)
            return self.fake_child

        with self._patch_spawn_and_acp():
            with mock.patch.object(peer_mod, "_spawn_acp_child", side_effect=_capture_spawn):
                with mock.patch.object(peer_mod, "_serve_control_plane", side_effect=_serve_once):
                    with mock.patch.object(
                        peer_mod, "require_probed_platform_for_live", return_value=None
                    ):
                        with mock.patch.object(peer_mod, "check_version", return_value="0.0.0"):
                            peer_mod.run_peer_start(ns)

        self.assertEqual(len(running_holder), 1)
        self.assertEqual(len(spawn_kwargs_holder), 1)
        policy = running_holder[0]["policy"]
        expected = _policy_field(code_mode._TOOLS, False)
        self.assertEqual(policy, expected)
        self.assertEqual(policy["tools"], grokcli.effective_tools(code_mode._TOOLS, False))
        self.assertEqual(policy["permissionMode"], grokcli.HEADLESS_PERMISSION_MODE)
        self.assertFalse(policy["subagents"])
        self.assertFalse(policy["memory"])
        self.assertFalse(policy["webAccess"])
        # Spawn must receive the same tools/web_access the envelope advertises.
        self.assertEqual(spawn_kwargs_holder[0].get("tools"), code_mode._TOOLS)
        self.assertEqual(spawn_kwargs_holder[0].get("web_access"), False)
        # Capability detection is honest: no false "deny hook registered" wording.
        progress_path = runstate.state_root() / "runs" / running_holder[0]["runId"] / "progress.jsonl"
        if progress_path.is_file():
            progress_text = progress_path.read_text(encoding="utf-8")
            self.assertNotIn("pre_tool_use deny hook registered", progress_text)


if __name__ == "__main__":
    unittest.main()
