# wrapper/scripts/tests/test_session_store.py
#
# Session-store archive/seed unit tests (Task 2.1) and lifecycle ordering
# proofs that archive runs strictly before private-home destroy and that
# archive failure only warns (never flips a successful run).

import os
import pathlib
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from groklib import runstate, session_store
from groklib.authhome import PrivateHome
from groklib.grokcli import GrokRunResult
from groklib.modes import _shared
from groklib.modes._envelope import ModeRun
from groklib.modes._shared import _run_grok_mode_body
from groklib.progress import ProgressWriter


class TestSessionStore(unittest.TestCase):
    def test_archive_and_seed_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            (home / ".grok" / "sessions").mkdir(parents=True)
            (home / ".grok" / "sessions" / "abc.jsonl").write_text("{}\n", encoding="utf-8")
            run_dir = pathlib.Path(td) / "run"
            run_dir.mkdir()
            meta = session_store.archive_session(home, run_dir, "abc")
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta["grokSessionId"], "abc")
            self.assertEqual(meta["schemaVersion"], 1)
            self.assertIn("archivedAtUtc", meta)
            archived = run_dir / "session" / "sessions" / "abc.jsonl"
            self.assertTrue(archived.is_file())
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE((run_dir / "session").stat().st_mode), 0o700
                )
                self.assertEqual(
                    stat.S_IMODE((run_dir / "session" / "sessions").stat().st_mode),
                    0o700,
                )
                self.assertEqual(stat.S_IMODE(archived.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE((run_dir / "session" / "session-meta.json").stat().st_mode),
                    0o600,
                )
            loaded = session_store.load_session_meta(run_dir)
            self.assertEqual(loaded, meta)
            home2 = pathlib.Path(td) / "home2"
            home2.mkdir()
            self.assertTrue(session_store.seed_sessions(run_dir, home2))
            self.assertTrue((home2 / ".grok" / "sessions" / "abc.jsonl").is_file())

    def test_archive_missing_sessions_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            home.mkdir()
            run_dir = pathlib.Path(td) / "run"
            run_dir.mkdir()
            self.assertIsNone(session_store.archive_session(home, run_dir, "abc"))
            self.assertIsNone(session_store.load_session_meta(run_dir))
            self.assertFalse(session_store.seed_sessions(run_dir, home))

    def test_archive_refuses_symlinked_sessions_root(self) -> None:
        # A poisoned ~/.grok/sessions root that is itself a symlink must not be
        # followed into the retained run archive (per-entry ignore only covers
        # children, not the root).
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            home = root / "home"
            outside = root / "outside-secret"
            outside.mkdir()
            (outside / "stolen.txt").write_text("secret-material\n", encoding="utf-8")
            grok = home / ".grok"
            grok.mkdir(parents=True)
            (grok / "sessions").symlink_to(outside, target_is_directory=True)
            run_dir = root / "run"
            run_dir.mkdir()
            meta = session_store.archive_session(home, run_dir, "abc")
            self.assertIsNone(meta)
            archived = run_dir / "session" / "sessions"
            self.assertFalse(archived.exists())
            self.assertFalse((run_dir / "session" / "sessions" / "stolen.txt").exists())


def _make_mode_run(**overrides) -> ModeRun:
    fields = dict(
        mode="review",
        binary=Path("/nonexistent/grok"),
        requested_model="grok-4.5",
        web_access=False,
        output_schema=None,
        timeout_seconds=30,
        max_turns=None,
        prompt_text="x",
        cwd=Path("."),
        tools=("read_file",),
        instructions=[],
        repository=None,
        target_workspace=None,
        detect_unexpected_edits=False,
    )
    fields.update(overrides)
    return ModeRun(**fields)


class SessionArchiveLifecycleTests(unittest.TestCase):
    """Archive ordering and soft-failure proofs against _run_grok_mode_body."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-session-life-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _private_home(self) -> PrivateHome:
        private_home = PrivateHome(
            home_dir=Path(self.tmp_root) / "fake-home",
            grok_dir=Path(self.tmp_root) / "fake-home" / ".grok",
            config_path=Path(self.tmp_root) / "fake-home" / ".grok" / "config.toml",
        )
        private_home.home_dir.mkdir(parents=True)
        private_home.grok_dir.mkdir(parents=True)
        return private_home

    def _fake_result(self) -> GrokRunResult:
        return GrokRunResult(
            argv=("/nonexistent/grok",),
            exit_status=0,
            stdout="{}",
            stderr="",
            duration_seconds=0.01,
            parsed={"usage": {}, "num_turns": 1},
            stop_reason="end_turn",
            session_id="result-sess",
            request_id="req",
            model_usage=None,
            effective_model="grok-4.5",
            final_text="ok",
            structured=None,
        )

    def _success_patches(self, private_home, fake_execute, destroy_side_effect=None):
        destroy_return = {"status": "clean", "detail": None}
        destroy_mock = mock.Mock(
            side_effect=destroy_side_effect,
            return_value=destroy_return,
        )
        if destroy_side_effect is None:
            destroy_mock = mock.Mock(return_value=destroy_return)
        return (
            mock.patch.object(_shared, "create_private_home", return_value=private_home),
            mock.patch("groklib.preflight_cache.ensure_ready", return_value=None),
            mock.patch(
                "groklib.platformsupport.require_probed_platform_for_live",
                return_value=None,
            ),
            mock.patch.object(_shared, "_execute_and_verify", side_effect=fake_execute),
            mock.patch.object(
                _shared, "policy_for_mode", return_value=types.SimpleNamespace()
            ),
            mock.patch.object(_shared, "render_sandbox_toml", return_value=""),
            mock.patch.object(_shared, "render_config_toml", return_value=""),
            mock.patch(
                "groklib.modes._envelope.destroy_private_home",
                destroy_mock,
            ),
            mock.patch.object(_shared, "_capture_review_fs_baseline", return_value=None),
            mock.patch.object(_shared, "_report_repo_fs_drift", return_value=None),
        )

    def test_archive_runs_before_private_home_destroy(self) -> None:
        paths = runstate.create_run("review")
        progress = ProgressWriter(paths.run_id, paths.progress_path)
        run = _make_mode_run(cwd=Path(self.tmp_root), session_id="spec-sess-1")
        private_home = self._private_home()
        fake_result = self._fake_result()
        sandbox_obj = {
            "requestedProfile": "read-only",
            "reportedProfile": "read-only",
            "enforced": True,
            "evidence": "test",
        }
        order: list = []

        def _fake_execute(run_arg, home, *args, **kwargs):
            sessions = home.home_dir / ".grok" / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "prompt_history.jsonl").write_text("{}\n", encoding="utf-8")
            result_holder = args[4] if len(args) >= 5 else kwargs.get("result_holder")
            if result_holder is not None:
                result_holder[0] = fake_result
            return fake_result, sandbox_obj, "grok-4.5"

        real_archive = session_store.archive_session

        def _tracking_archive(home_dir, run_dir, session_id):
            order.append(("archive", session_id))
            return real_archive(home_dir, run_dir, session_id)

        def _tracking_destroy(self_hc):
            order.append(("destroy", None))
            return {"status": "clean", "detail": None}

        patches = self._success_patches(private_home, _fake_execute)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
            5
        ], patches[6], patches[7], patches[8], patches[9], mock.patch.object(
            session_store, "archive_session", side_effect=_tracking_archive
        ), mock.patch.object(
            _shared.HomeCleanup, "destroy_once", side_effect=_tracking_destroy, autospec=True
        ):
            envelope = _run_grok_mode_body(run, paths, progress, [None], [None])

        self.assertEqual(envelope["status"], "success", envelope)
        self.assertGreaterEqual(len(order), 2, order)
        self.assertEqual(order[0][0], "archive", order)
        self.assertEqual(order[0][1], "spec-sess-1", order)
        self.assertEqual(order[1][0], "destroy", order)
        meta = session_store.load_session_meta(paths.run_dir)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta["grokSessionId"], "spec-sess-1")
        self.assertTrue(
            (paths.run_dir / "session" / "sessions" / "prompt_history.jsonl").is_file()
        )

    def test_archive_failure_only_warns(self) -> None:
        paths = runstate.create_run("review")
        progress = ProgressWriter(paths.run_id, paths.progress_path)
        run = _make_mode_run(cwd=Path(self.tmp_root), session_id="spec-sess-2")
        private_home = self._private_home()
        fake_result = self._fake_result()
        sandbox_obj = {
            "requestedProfile": "read-only",
            "reportedProfile": "read-only",
            "enforced": True,
            "evidence": "test",
        }

        def _fake_execute(run_arg, home, *args, **kwargs):
            sessions = home.home_dir / ".grok" / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "x.jsonl").write_text("{}\n", encoding="utf-8")
            result_holder = args[4] if len(args) >= 5 else kwargs.get("result_holder")
            if result_holder is not None:
                result_holder[0] = fake_result
            return fake_result, sandbox_obj, "grok-4.5"

        patches = self._success_patches(private_home, _fake_execute)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[
            5
        ], patches[6], patches[7], patches[8], patches[9], mock.patch.object(
            session_store,
            "archive_session",
            side_effect=OSError("disk full"),
        ):
            envelope = _run_grok_mode_body(run, paths, progress, [None], [None])

        self.assertEqual(envelope["status"], "success", envelope)
        warnings = envelope.get("warnings") or []
        self.assertTrue(
            any("session archive" in str(w).lower() for w in warnings),
            warnings,
        )


if __name__ == "__main__":
    unittest.main()
