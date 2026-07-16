# wrapper/scripts/tests/test_runstate.py

import ast
import json
import os
import pathlib
import re
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from groklib import GrokWrapperError, platformsupport, runstate
from groklib.runstate import LeaderSocketPathTooLong, StateOwnershipError, UnknownRunError

_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


def _dead_pid() -> int:
    """Spawn a child, wait for it to exit, and return its now-dead pid.

    A reapable home requires a liveness lease whose owner is PROVABLY dead
    (Grok dogfood-4 #3 fail-safe: a home with NO/unknown lease is treated as
    possibly-active and never reaped). Tests give reapable homes a dead-pid lease.
    """
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class RunstateTests(unittest.TestCase):
    """Covers the C5 runstate public API against a fully isolated XDG_STATE_HOME."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-runstate-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _new_scratch_dir(self) -> pathlib.Path:
        return pathlib.Path(tempfile.mkdtemp(dir=self.tmp_root))

    def test_new_run_id_matches_contract_regex(self) -> None:
        run_id = runstate.new_run_id()
        self.assertRegex(run_id, _RUN_ID_RE)
        self.assertTrue(runstate.is_valid_run_id(run_id))

    def test_run_ids_are_unique_across_1000_generations(self) -> None:
        # new_run_id() combines a 1-second-resolution timestamp with 24 bits
        # of randomness (secrets.token_hex(3)). Real runs are minted one at a
        # time, never 1000 within the same wall-clock second, so this
        # advances the clock deterministically per call to reproduce that
        # real usage pattern while still exercising the real implementation
        # end to end (format, uniqueness) with the exact same strict check.
        fake_gmtimes = [time.gmtime(seconds) for seconds in range(1000)]
        with mock.patch("time.gmtime", side_effect=fake_gmtimes):
            generated = [runstate.new_run_id() for _ in range(1000)]

        self.assertEqual(len(generated), 1000)
        self.assertEqual(len(set(generated)), 1000)
        for run_id in generated:
            self.assertRegex(run_id, _RUN_ID_RE)

        # Deterministic sub-check: hold the clock at a single constant value
        # (removing timestamp variation entirely) and confirm the 6-hex-char
        # suffix (secrets.token_hex(3), 24 bits of randomness) still varies
        # across generations. This catches a regression where the suffix
        # generator silently degenerates to a constant value. It deliberately
        # does NOT assert all-64-unique: a small collision probability across
        # 64 draws from 24 bits of randomness is expected by the C1 formula,
        # and asserting full uniqueness here would reintroduce the same
        # birthday-paradox flake this test's clock-advancing design avoids.
        with mock.patch("time.gmtime", return_value=time.gmtime(0)):
            constant_clock_ids = [runstate.new_run_id() for _ in range(64)]
        suffixes = [run_id.split("-")[1] for run_id in constant_clock_ids]
        self.assertGreater(len(set(suffixes)), 1)

    def test_create_run_builds_c2_layout_with_0700_dirs(self) -> None:
        paths = runstate.create_run("review")

        self.assertTrue(runstate.is_valid_run_id(paths.run_id))
        self.assertTrue(paths.run_dir.is_dir())
        self.assertTrue(paths.trace_dir.is_dir())

        owner_path = paths.run_dir / "owner.json"
        self.assertTrue(owner_path.is_file())

        # POSIX-octal permission modes (D-PORT): the platform-neutral
        # existence/type coverage for these three paths already runs
        # unconditionally above (paths.run_dir.is_dir(),
        # paths.trace_dir.is_dir(), owner_path.is_file()); only the exact
        # 0o700/0o600 mode check is POSIX-only and guarded here so the
        # suite does not hard-fail on Windows.
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(paths.run_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(paths.trace_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(owner_path.stat().st_mode), 0o600)

        self.assertEqual(paths.progress_path, paths.run_dir / "progress.jsonl")
        self.assertEqual(paths.envelope_path, paths.run_dir / "envelope.json")
        self.assertEqual(paths.trace_dir, paths.run_dir / "trace")

    def test_create_run_collision_fails_closed(self) -> None:
        # A colliding run id must never adopt the existing run directory.
        # Pin new_run_id() to a fixed value so the second create_run() call
        # collides with the first one's already-created leaf directory.
        fixed_run_id = "20260101T000000Z-abcdef"
        with mock.patch("groklib.runstate.new_run_id", return_value=fixed_run_id):
            first_paths = runstate.create_run("review")
            self.assertEqual(first_paths.run_id, fixed_run_id)

            owner_path = first_paths.run_dir / "owner.json"
            owner_bytes_before = owner_path.read_bytes()

            with self.assertRaises(StateOwnershipError):
                runstate.create_run("review")

            owner_bytes_after = owner_path.read_bytes()

        self.assertEqual(owner_bytes_before, owner_bytes_after)

    def _capture_fd2(self, thunk):
        """Run ``thunk`` with fd 2 redirected to a temp file; return (result, stderr_text)."""
        with tempfile.TemporaryFile() as capture:
            saved = os.dup(2)
            try:
                os.dup2(capture.fileno(), 2)
                result = thunk()
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            capture.seek(0)
            return result, capture.read().decode("utf-8")

    def test_emit_run_id_marker_writes_machine_readable_stderr_line(self) -> None:
        # F-RELAY-RUNID: the marker is a single, stable, machine-readable stderr
        # line the plugin relay parses. stdout is untouched.
        run_id = "20260715T010203Z-abc123"
        _, stderr_text = self._capture_fd2(lambda: runstate.emit_run_id_marker(run_id))
        self.assertEqual(stderr_text.strip(), "[grok-run-id] {}".format(run_id))

    def test_create_run_emits_run_id_marker_to_stderr(self) -> None:
        paths, stderr_text = self._capture_fd2(lambda: runstate.create_run("review"))
        self.assertIn("[grok-run-id] {}".format(paths.run_id), stderr_text)

    def test_state_root_treats_whitespace_only_xdg_as_unset(self) -> None:
        # F-STATE-WS: a whitespace-only XDG_STATE_HOME must fall back to the
        # default, not yield base=Path("   ").
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "   \t "}):
            root = runstate.state_root()
        expected = pathlib.Path.home() / ".local" / "state" / "grok-skills"
        self.assertEqual(root, expected)

    def test_state_root_rejects_relative_xdg_and_falls_back(self) -> None:
        # F-STATE-ABS: a RELATIVE XDG_STATE_HOME is invalid per the XDG spec and
        # must be ignored, or run state would be written under the process CWD (a
        # live worktree). It must fall back to the absolute default.
        expected = pathlib.Path.home() / ".local" / "state" / "grok-skills"
        for relative in (".state", "state", "sub/dir", "./here"):
            with self.subTest(relative=relative):
                with mock.patch.dict(os.environ, {"XDG_STATE_HOME": relative}):
                    root = runstate.state_root()
                self.assertTrue(root.is_absolute(), "state root must be absolute")
                self.assertEqual(root, expected)

    def test_owner_marker_roundtrip_and_exact_owner_string(self) -> None:
        marker_dir = self._new_scratch_dir()
        run_id = runstate.new_run_id()

        runstate.write_owner_marker(marker_dir, run_id)

        marker_path = marker_dir / "owner.json"
        self.assertTrue(marker_path.is_file())
        # POSIX-octal permission mode (D-PORT): marker_path.is_file() above
        # already gives platform-neutral existence/type coverage; only the
        # exact 0o600 mode check is POSIX-only.
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)

        returned_run_id = runstate.verify_owner_marker(marker_path)
        self.assertEqual(returned_run_id, run_id)

        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["owner"], "grok-skills-wrapper")
        self.assertEqual(payload["runId"], run_id)
        self.assertIn("createdAtUtc", payload)

    def test_write_owner_marker_file_arbitrary_path(self) -> None:
        # write_owner_marker_file must accept the marker file path itself
        # (not a directory), e.g. Task 8's <worktree-path>.owner.json shape.
        scratch_dir = self._new_scratch_dir()
        marker_path = scratch_dir / "wt.owner.json"
        run_id = runstate.new_run_id()

        runstate.write_owner_marker_file(marker_path, run_id)

        self.assertTrue(marker_path.is_file())
        # POSIX-octal permission mode (D-PORT): marker_path.is_file() above
        # already gives platform-neutral existence/type coverage; only the
        # exact 0o600 mode check is POSIX-only.
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)

        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["owner"], "grok-skills-wrapper")
        self.assertEqual(payload["runId"], run_id)
        self.assertIn("createdAtUtc", payload)

        self.assertEqual(runstate.verify_owner_marker(marker_path), run_id)

    def test_verify_owner_marker_rejects_wrong_owner(self) -> None:
        marker_dir = self._new_scratch_dir()
        marker_path = marker_dir / "owner.json"
        payload = {
            "schemaVersion": 1,
            "owner": "someone-else",
            "runId": runstate.new_run_id(),
            "createdAtUtc": "2026-01-01T00:00:00+00:00",
        }
        marker_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(StateOwnershipError):
            runstate.verify_owner_marker(marker_path)

    def test_verify_owner_marker_rejects_missing_marker(self) -> None:
        marker_dir = self._new_scratch_dir()
        missing_marker = marker_dir / "owner.json"

        with self.assertRaises(StateOwnershipError):
            runstate.verify_owner_marker(missing_marker)

    def test_load_run_record_unknown_id_raises(self) -> None:
        with self.assertRaises(UnknownRunError):
            runstate.load_run_record(runstate.new_run_id())

        with self.assertRaises(UnknownRunError):
            runstate.load_run_record("not-a-valid-run-id")

    def test_load_run_record_non_object_json_is_classified_not_attributeerror(self) -> None:
        # Grok dogfood #8: a run.json that parses but is not an object (e.g. "[]")
        # must raise the classified UnknownRunError (invalid-target) under the
        # REQUESTED run id, not let a later record.get(...) AttributeError escape.
        run_id = runstate.new_run_id()
        run_dir = runstate.state_root() / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(UnknownRunError) as ctx:
            runstate.load_run_record(run_id)
        self.assertEqual(ctx.exception.error_class, "invalid-target")
        self.assertEqual(ctx.exception.detail.get("runId"), run_id)

    def test_is_valid_run_id_rejects_traversal_and_ref_unsafe_input(self) -> None:
        # A fixed uppercase-hex candidate (rather than new_run_id().upper())
        # so the negative case is deterministic: if the 3 random bytes ever
        # render as all-digit hex, .upper() would be a no-op and silently
        # degenerate this into a valid-id case.
        invalid_candidates = [
            "../x",
            "a/b",
            "HEAD",
            "",
            "20260101T000000Z-ABCDEF",
        ]
        for candidate in invalid_candidates:
            with self.subTest(candidate=candidate):
                self.assertFalse(runstate.is_valid_run_id(candidate))

    def test_list_run_ids_newest_first(self) -> None:
        run_ids = [
            "20260101T000000Z-aaaaaa",
            "20260102T120000Z-bbbbbb",
            "20260101T235959Z-cccccc",
        ]
        for run_id in run_ids:
            run_dir = runstate.state_root() / "runs" / run_id
            run_dir.mkdir(parents=True)

        listed = runstate.list_run_ids()
        self.assertEqual(listed, sorted(run_ids, reverse=True))

    def test_allocate_leader_socket_under_private_home_and_length_guard(self) -> None:
        # allocate_leader_socket does pure path arithmetic (no filesystem
        # access), so a short synthetic path exercises the success case
        # without depending on the OS temp dir's real (often long, e.g.
        # macOS /var/folders/...) depth.
        run_id = runstate.new_run_id()
        short_home = pathlib.Path("/tmp/g")

        socket_path = runstate.allocate_leader_socket(short_home, run_id)

        # The socket filename uses only the run-id's 6-hex tail, not the full
        # run id: the full id is not load-bearing (nothing parses it back), and
        # the short name keeps the path under the AF_UNIX byte guard.
        suffix = run_id.rsplit("-", 1)[-1]
        self.assertEqual(socket_path, short_home / ".grok" / "l-{}.sock".format(suffix))
        self.assertLess(len(str(socket_path).encode("utf-8")), 100)

        long_home = short_home / ("x" * 200)
        with self.assertRaises(LeaderSocketPathTooLong):
            runstate.allocate_leader_socket(long_home, run_id)

    def test_audit_stale_temp_homes_removes_only_owned_expired_dirs(self) -> None:
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)

        old_ts = 0.0  # 1970-01-01, always older than any max_age_seconds we pass

        def _make_home(name: str) -> pathlib.Path:
            home = pathlib.Path(fake_temp_root) / name
            home.mkdir(mode=0o700)
            os.chmod(home, 0o700)
            return home

        # A reapable home needs a dead-pid liveness lease (Grok dogfood-4 #3): a
        # home with no lease is possibly-active and never reaped. The not-reapable
        # homes below ALSO get a dead-pid lease so their survival is proven to be
        # due to owner/mode gating, not merely a missing lease.
        dead_pid = _dead_pid()

        expired_owned = _make_home("gs-expired-owned")
        runstate.write_owner_marker(expired_owned, runstate.new_run_id())
        runstate.write_home_liveness_marker(expired_owned, dead_pid)
        os.utime(expired_owned, (old_ts, old_ts))

        fresh_owned = _make_home("gs-fresh-owned")
        runstate.write_owner_marker(fresh_owned, runstate.new_run_id())
        runstate.write_home_liveness_marker(fresh_owned, dead_pid)
        # mtime left at creation time (now); must NOT be treated as stale.

        expired_wrong_owner = _make_home("gs-expired-wrong-owner")
        (expired_wrong_owner / "owner.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "owner": "not-the-wrapper",
                    "runId": runstate.new_run_id(),
                    "createdAtUtc": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        runstate.write_home_liveness_marker(expired_wrong_owner, dead_pid)
        os.utime(expired_wrong_owner, (old_ts, old_ts))

        expired_wrong_mode = _make_home("gs-expired-wrong-mode")
        runstate.write_owner_marker(expired_wrong_mode, runstate.new_run_id())
        runstate.write_home_liveness_marker(expired_wrong_mode, dead_pid)
        os.chmod(expired_wrong_mode, 0o755)
        os.utime(expired_wrong_mode, (old_ts, old_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [str(expired_owned)])
        self.assertFalse(expired_owned.exists())
        self.assertTrue(fresh_owned.exists())
        self.assertTrue(expired_wrong_owner.exists())
        self.assertTrue(expired_wrong_mode.exists())

    def test_reap_never_removes_home_with_live_owner_pid(self) -> None:
        # Grok dogfood-3 #1: an ACTIVE run's private home must NEVER be reaped, even
        # when its mtime is older than the reap window (a long --timeout run). The
        # owner-pid liveness lease is the live signal age cannot be.
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-livepid-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        old_ts = 0.0  # older than any window

        live_home = pathlib.Path(fake_temp_root) / "gs-live"
        live_home.mkdir(mode=0o700)
        os.chmod(live_home, 0o700)
        runstate.write_owner_marker(live_home, runstate.new_run_id())
        # The current test process is unquestionably alive.
        runstate.write_home_liveness_marker(live_home, os.getpid())
        os.utime(live_home, (old_ts, old_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [], "an active (live-pid) home must not be reaped")
        self.assertTrue(live_home.exists(), "the live run's credential-bearing home must survive")

    def test_reap_removes_expired_home_with_dead_owner_pid(self) -> None:
        # A genuinely dead run (its owner pid is gone) still reaps by age, so a
        # crashed run's stranded credential home is not leaked forever.
        import subprocess
        import sys

        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-deadpid-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        old_ts = 0.0

        # A child that exits immediately, then is reaped: its pid is now dead.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        dead_pid = child.pid

        dead_home = pathlib.Path(fake_temp_root) / "gs-dead"
        dead_home.mkdir(mode=0o700)
        os.chmod(dead_home, 0o700)
        runstate.write_owner_marker(dead_home, runstate.new_run_id())
        runstate.write_home_liveness_marker(dead_home, dead_pid)
        os.utime(dead_home, (old_ts, old_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [str(dead_home)], "a dead run's stale home must still reap")
        self.assertFalse(dead_home.exists())

    def test_reap_skips_home_with_unreadable_lease_as_possibly_active(self) -> None:
        # Grok dogfood-4 #3 reaper-lease-fail-safe: a home with an owner marker and
        # an mtime older than the reap WINDOW but a MISSING/UNREADABLE liveness lease
        # is POSSIBLY-ACTIVE (its lease write may have failed while it is still
        # running) and must NEVER be reaped by age within the hard cap -- the reaper
        # must not fall back to age-only. The mtime here is older than max_age_seconds
        # (3600s) yet WELL within the unknown-lease hard cap, so the fail-safe holds.
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-nolease-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        within_cap_ts = time.time() - 2 * 3600  # older than the 3600s window, within the hard cap

        for name, lease_bytes in (("gs-nolease", None), ("gs-corruptlease", b"{not json")):
            home = pathlib.Path(fake_temp_root) / name
            home.mkdir(mode=0o700)
            os.chmod(home, 0o700)
            runstate.write_owner_marker(home, runstate.new_run_id())
            if lease_bytes is not None:
                (home / runstate.TEMP_HOME_LIVENESS_FILENAME).write_bytes(lease_bytes)
            os.utime(home, (within_cap_ts, within_cap_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [], "a home with no/unreadable lease is possibly-active; never reaped")
        self.assertTrue((pathlib.Path(fake_temp_root) / "gs-nolease").exists())
        self.assertTrue((pathlib.Path(fake_temp_root) / "gs-corruptlease").exists())

    def test_reap_removes_unknown_lease_home_older_than_hard_cap(self) -> None:
        # Grok r5 #5 unknown-lease-hard-cap: a home whose liveness lease could not be
        # written is UNKNOWN (possibly-active) and is NOT reaped within the live
        # window -- but once it is OLDER than the hard cap (max permitted --timeout
        # plus a margin) it cannot belong to any still-running run, so its stranded
        # copy of the operator's Grok auth material IS reaped. A within-cap
        # unknown-lease home is still protected (fail-safe preserved).
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-hardcap-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        now = time.time()
        past_cap_ts = now - runstate.UNKNOWN_LEASE_HARD_REAP_AGE_SECONDS - 3600
        within_cap_ts = now - runstate.UNKNOWN_LEASE_HARD_REAP_AGE_SECONDS + 24 * 3600

        reaped = pathlib.Path(fake_temp_root) / "gs-unknown-past-cap"
        reaped.mkdir(mode=0o700)
        os.chmod(reaped, 0o700)
        runstate.write_owner_marker(reaped, runstate.new_run_id())  # valid marker, NO liveness lease
        os.utime(reaped, (past_cap_ts, past_cap_ts))

        kept = pathlib.Path(fake_temp_root) / "gs-unknown-within-cap"
        kept.mkdir(mode=0o700)
        os.chmod(kept, 0o700)
        runstate.write_owner_marker(kept, runstate.new_run_id())
        os.utime(kept, (within_cap_ts, within_cap_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(
                max_age_seconds=runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS
            )

        self.assertEqual(removed, [str(reaped)], "an unknown-lease home older than the hard cap IS reaped")
        self.assertFalse(reaped.exists())
        self.assertTrue(kept.exists(), "an unknown-lease home within the hard cap is still protected")

    def test_reap_removes_home_when_pid_recycled_onto_another_process(self) -> None:
        # F2/F4 pid-liveness-lease-reuse: a lease whose pid is now ALIVE but belongs
        # to a DIFFERENT process (identity token mismatch) is a DEAD run whose pid
        # was recycled; it must reap, never look permanently alive.
        if platformsupport.process_start_token(os.getpid()) is None:
            self.skipTest("process start-time token unavailable on this host")
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-recycled-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        old_ts = 0.0

        home = pathlib.Path(fake_temp_root) / "gs-recycled"
        home.mkdir(mode=0o700)
        os.chmod(home, 0o700)
        runstate.write_owner_marker(home, runstate.new_run_id())
        # A live pid (this process) but a STALE identity token: the original owner
        # that wrote the lease is gone; this pid was recycled.
        (home / runstate.TEMP_HOME_LIVENESS_FILENAME).write_text(
            json.dumps({"schemaVersion": 1, "pid": os.getpid(), "startToken": "stale-token-of-a-dead-process"}),
            encoding="utf-8",
        )
        os.utime(home, (old_ts, old_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [str(home)], "a recycled-pid home is dead and must reap")
        self.assertFalse(home.exists())

    def test_reap_never_removes_live_home_with_matching_identity_token(self) -> None:
        # The pid-reuse guard must not reap a genuinely LIVE run: a lease written by
        # write_home_liveness_marker for THIS process carries the matching token, so
        # it is protected even with an old mtime.
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-liveid-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        home = pathlib.Path(fake_temp_root) / "gs-liveid"
        home.mkdir(mode=0o700)
        os.chmod(home, 0o700)
        runstate.write_owner_marker(home, runstate.new_run_id())
        runstate.write_home_liveness_marker(home, os.getpid())
        os.utime(home, (0.0, 0.0))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [], "a live home with a matching identity token must never reap")
        self.assertTrue(home.exists())

    def test_is_orphaned_partial_run_dir_reaps_dead_post_create_crash_only(self) -> None:
        # F4-partial-create: a run dir with a VALID owner marker but NO run.json is
        # reapable ONLY when its owner is provably dead (a post-create crash);
        # a live or unknown owner is an in-flight create and must be protected.
        def _seed():
            run_id = runstate.new_run_id()
            run_dir = runstate.state_root() / "runs" / run_id
            run_dir.mkdir(parents=True, mode=0o700)
            os.chmod(run_dir, 0o700)
            runstate.write_owner_marker(run_dir, run_id)
            return run_id, run_dir

        # (a) dead owner lease -> reapable
        dead_run_id, dead_dir = _seed()
        runstate.write_home_liveness_marker(dead_dir, _dead_pid())
        self.assertTrue(runstate.is_orphaned_partial_run_dir(dead_run_id))

        # (b) live owner lease -> NOT reapable (in-flight)
        live_run_id, live_dir = _seed()
        runstate.write_home_liveness_marker(live_dir, os.getpid())
        self.assertFalse(runstate.is_orphaned_partial_run_dir(live_run_id))

        # (c) no lease (unknown) -> NOT reapable (possibly-active, conservative)
        unknown_run_id, _unknown_dir = _seed()
        self.assertFalse(runstate.is_orphaned_partial_run_dir(unknown_run_id))

        # (d) no valid marker at all -> pre-marker debris, reapable
        pre_run_id = runstate.new_run_id()
        pre_dir = runstate.state_root() / "runs" / pre_run_id
        pre_dir.mkdir(parents=True, mode=0o700)
        os.chmod(pre_dir, 0o700)
        self.assertTrue(runstate.is_orphaned_partial_run_dir(pre_run_id))

    def test_audit_skips_symlink_candidates_and_continues(self) -> None:
        # A symlink candidate must never be handed to shutil.rmtree (which
        # refuses to operate on a symlink and would abort the whole audit).
        # It must be skipped, and the audit must still remove the other,
        # real, expired-and-owned candidate in the same pass.
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-symlink-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)

        old_ts = 0.0

        def _make_home(name: str) -> pathlib.Path:
            home = pathlib.Path(fake_temp_root) / name
            home.mkdir(mode=0o700)
            os.chmod(home, 0o700)
            return home

        dead_pid = _dead_pid()

        expired_owned = _make_home("gs-expired-owned")
        runstate.write_owner_marker(expired_owned, runstate.new_run_id())
        runstate.write_home_liveness_marker(expired_owned, dead_pid)
        os.utime(expired_owned, (old_ts, old_ts))

        # Symlink target lives outside the gs-* prefix so it is
        # not itself picked up as a second, independent candidate.
        symlink_target = pathlib.Path(fake_temp_root) / "symlink-target-owned"
        symlink_target.mkdir(mode=0o700)
        os.chmod(symlink_target, 0o700)
        runstate.write_owner_marker(symlink_target, runstate.new_run_id())
        runstate.write_home_liveness_marker(symlink_target, dead_pid)
        os.utime(symlink_target, (old_ts, old_ts))

        symlink_candidate = pathlib.Path(fake_temp_root) / "gs-symlink"
        symlink_candidate.symlink_to(symlink_target, target_is_directory=True)

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [str(expired_owned)])
        self.assertFalse(expired_owned.exists())
        self.assertTrue(symlink_candidate.is_symlink())
        self.assertTrue(symlink_target.exists())

    def test_best_effort_reap_removes_stale_owner_marked_homes_only(self) -> None:
        # Grok dogfood-2 #1/#7: the live-start reap wrapper removes an EXPIRED,
        # owner-marked, owned home but spares a fresh one and an unmarked one, and
        # never raises.
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-faketemp-reap-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        old_ts = 0.0

        def _make_home(name: str) -> pathlib.Path:
            home = pathlib.Path(fake_temp_root) / name
            home.mkdir(mode=0o700)
            os.chmod(home, 0o700)
            return home

        expired_marked = _make_home("gs-expired-marked")
        runstate.write_owner_marker(expired_marked, runstate.new_run_id())
        runstate.write_home_liveness_marker(expired_marked, _dead_pid())
        os.utime(expired_marked, (old_ts, old_ts))

        fresh_marked = _make_home("gs-fresh-marked")
        runstate.write_owner_marker(fresh_marked, runstate.new_run_id())
        # A LIVE-pid lease proves the fresh home survives because its owner is
        # alive (an active run), not merely because a lease is missing.
        runstate.write_home_liveness_marker(fresh_marked, os.getpid())  # mtime = now

        expired_unmarked = _make_home("gs-expired-unmarked")  # no owner.json, no lease
        os.utime(expired_unmarked, (old_ts, old_ts))

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            removed = runstate.best_effort_reap_stale_temp_homes(max_age_seconds=3600)

        self.assertEqual(removed, [str(expired_marked)])
        self.assertFalse(expired_marked.exists())
        self.assertTrue(fresh_marked.exists(), "an active run's fresh home must never be reaped")
        self.assertTrue(expired_unmarked.exists(), "an unmarked/foreign dir must never be reaped")

    def test_best_effort_reap_swallows_failure(self) -> None:
        # A reap failure must never abort the caller (the live run start).
        with mock.patch.object(
            runstate, "audit_stale_temp_homes", side_effect=GrokWrapperError("cleanup-failure", "boom", {})
        ):
            self.assertEqual(runstate.best_effort_reap_stale_temp_homes(max_age_seconds=3600), [])

    def test_live_start_reap_window_exceeds_longest_mode_timeout(self) -> None:
        # The window must be safely LARGER than the longest live-mode wall-clock
        # timeout (code: 3600s) so a concurrently-active run's home is never reaped.
        self.assertGreater(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS, 3600)

    def test_runstate_import_isolation(self) -> None:
        module_path = pathlib.Path(__file__).resolve().parent.parent / "groklib" / "runstate.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))

        forbidden = {
            "argparse",
            "groklib.modes",
            "groklib.grokcli",
            "groklib.sandbox",
            "groklib.rules",
            "groklib.envelope",
        }

        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.add(node.module)

        violations = imported_names & forbidden
        self.assertEqual(
            violations,
            set(),
            "runstate.py must not import {}; found {}".format(forbidden, violations),
        )


if __name__ == "__main__":
    unittest.main()


class CreateRunSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-seed-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def test_seed_lifecycle_created_status_running_revision_zero(self) -> None:
        paths = runstate.create_run("review")
        record = json.loads((paths.run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["lifecycle"], "created")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["recordRevision"], 0)
        self.assertEqual(record["runId"], paths.run_id)
        self.assertEqual(record["mode"], "review")

    def test_seed_exists_before_run_id_marker(self) -> None:
        order = []
        real_write = runstate.write_json_atomic
        real_emit = runstate.emit_run_id_marker

        def tracking_write(path, payload):
            if path.name == "run.json":
                order.append("seed")
            return real_write(path, payload)

        def tracking_emit(run_id):
            order.append("marker")
            return real_emit(run_id)

        with mock.patch.object(runstate, "write_json_atomic", side_effect=tracking_write):
            with mock.patch.object(runstate, "emit_run_id_marker", side_effect=tracking_emit):
                runstate.create_run("code")
        self.assertIn("seed", order)
        self.assertIn("marker", order)
        self.assertLess(order.index("seed"), order.index("marker"))

    def test_write_json_atomic_no_tmp_left(self) -> None:
        path = pathlib.Path(self.tmp_root) / "x.json"
        runstate.write_json_atomic(path, {"a": 1})
        runstate.write_json_atomic(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text())["a"], 2)
        self.assertEqual(list(path.parent.glob("x.json.tmp.*")), [])

    def test_write_json_atomic_fsyncs_parent_directory(self) -> None:
        """After os.replace, parent dir is fsync'd for power-loss durability."""
        path = pathlib.Path(self.tmp_root) / "durable.json"
        fsynced_fds = []
        real_fsync = os.fsync
        real_open = os.open

        def tracking_fsync(fd):
            fsynced_fds.append(fd)
            return real_fsync(fd)

        dir_fds = []

        def tracking_open(path_str, flags, *args, **kwargs):
            fd = real_open(path_str, flags, *args, **kwargs)
            # O_RDONLY open of parent (no O_WRONLY) is the dir fsync path
            if flags == os.O_RDONLY and path_str == str(path.parent):
                dir_fds.append(fd)
            return fd

        with mock.patch.object(os, "fsync", side_effect=tracking_fsync):
            with mock.patch.object(os, "open", side_effect=tracking_open):
                runstate.write_json_atomic(path, {"ok": True})
        self.assertTrue(dir_fds, "expected O_RDONLY open of parent directory")
        self.assertTrue(
            any(fd in fsynced_fds for fd in dir_fds),
            "expected fsync on parent directory fd after replace",
        )
        self.assertEqual(json.loads(path.read_text())["ok"], True)

    def test_cas_update_and_conflict(self) -> None:
        paths = runstate.create_run("review")
        updated = runstate.cas_update_run_record(paths, 0, {"repository": "/repo"})
        self.assertEqual(updated["recordRevision"], 1)
        self.assertEqual(updated["repository"], "/repo")
        self.assertEqual(updated["lifecycle"], "created")
        with self.assertRaises(runstate.CasConflictError):
            runstate.cas_update_run_record(paths, 0, {"repository": "/other"})

    def test_set_lifecycle_graph_and_terminal_refuse(self) -> None:
        paths = runstate.create_run("review")
        r = runstate.set_lifecycle(paths, 0, "running")
        self.assertEqual(r["lifecycle"], "running")
        r = runstate.set_lifecycle(paths, 1, "finalizing")
        self.assertEqual(r["lifecycle"], "finalizing")
        with self.assertRaises(runstate.LifecycleError):
            runstate.set_lifecycle(paths, 2, "completed")
        from groklib.envelope import build_envelope

        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        runstate.persist_terminal_envelope(paths, 2, env, lifecycle="completed")
        with self.assertRaises(runstate.LifecycleError):
            runstate.set_lifecycle(paths, 3, "running")

    def test_persist_terminal_envelope_first_and_idempotent(self) -> None:
        from groklib.envelope import build_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        runstate.persist_terminal_envelope(paths, 2, env, lifecycle="completed")
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "completed")
        self.assertTrue(paths.envelope_path.is_file())
        body = paths.envelope_path.read_bytes()
        # Second call with different envelope must not replace
        env2 = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": False})
        runstate.persist_terminal_envelope(paths, None, env2, lifecycle="completed")
        self.assertEqual(paths.envelope_path.read_bytes(), body)

    def test_persist_crash_recovery_finishes_lifecycle(self) -> None:
        from groklib.envelope import build_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        # Simulate crash after envelope write before lifecycle
        runstate.write_json_atomic(paths.envelope_path, env)
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "finalizing")
        runstate.persist_terminal_envelope(paths, None, None)
        record = runstate.load_run_record(paths.run_id)
        self.assertEqual(record["lifecycle"], "completed")


    def test_public_write_run_record_removed(self) -> None:
        self.assertFalse(hasattr(runstate, "write_run_record"))

    def test_cas_cannot_set_success_status_while_nonterminal(self) -> None:
        paths = runstate.create_run("review")
        updated = runstate.cas_update_run_record(
            paths, 0, {"requestedModel": "grok-4.5", "status": "success"}
        )
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["lifecycle"], "created")
        self.assertFalse(paths.envelope_path.is_file())

    def test_concurrent_cas_conflict(self) -> None:
        import threading
        paths = runstate.create_run("review")
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                runstate.cas_update_run_record(paths, 0, {"repository": "/r"})
                results.append("ok")
            except runstate.CasConflictError:
                results.append("conflict")

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(runstate.load_run_record(paths.run_id)["recordRevision"], 1)

    def test_effective_lifecycle_resolution_order(self) -> None:
        rec = {"lifecycle": "completed", "status": "success"}
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=True, envelope_status="failure", process_liveness="dead"
        )
        self.assertEqual(life, "completed")
        self.assertEqual(src, "record")
        rec = {"lifecycle": "finalizing", "status": "running"}
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=True, envelope_status="success", process_liveness="dead"
        )
        self.assertEqual(life, "completed")
        self.assertEqual(src, "envelope")
        life, src = runstate.effective_lifecycle(
            rec, has_valid_envelope=False, envelope_status=None, process_liveness="dead"
        )
        self.assertEqual(life, "interrupted")
        self.assertEqual(src, "derived")

    def test_persist_requires_revision_and_matching_lifecycle(self) -> None:
        from groklib.envelope import build_envelope, failure_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        runstate.set_lifecycle(paths, 1, "finalizing")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, None, env, lifecycle="completed")
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, 2, env, lifecycle=None)
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, 2, env, lifecycle="failed")
        fail = failure_envelope(
            run_id=paths.run_id, mode="review", error_class="cli-failure", message="x"
        )
        paths2 = runstate.create_run("code")
        runstate.set_lifecycle(paths2, 0, "running")
        runstate.persist_terminal_envelope(paths2, 1, fail, lifecycle="failed")
        self.assertEqual(runstate.load_run_record(paths2.run_id)["lifecycle"], "failed")

    def test_completed_from_running_refused(self) -> None:
        from groklib.envelope import build_envelope

        paths = runstate.create_run("review")
        runstate.set_lifecycle(paths, 0, "running")
        env = build_envelope(run_id=paths.run_id, mode="review", status="success", response={"ok": True})
        with self.assertRaises(runstate.LifecycleError):
            runstate.persist_terminal_envelope(paths, 1, env, lifecycle="completed")

    def test_cas_rejects_unknown_keys(self) -> None:
        paths = runstate.create_run("review")
        with self.assertRaises(runstate.LifecycleError):
            runstate.cas_update_run_record(paths, 0, {"notAField": 1})
