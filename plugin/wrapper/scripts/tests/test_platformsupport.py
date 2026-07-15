# wrapper/scripts/tests/test_platformsupport.py

import os
import pathlib
import signal
import subprocess
import sys
import stat
import tempfile
import shutil
import unittest
from unittest import mock

from groklib import GrokWrapperError, platformsupport


class CurrentPlatformTests(unittest.TestCase):
    """current_platform / is_posix mapping, including the injected non-host branches."""

    def test_current_platform_maps_sys_platform(self) -> None:
        # The pure mapping helper is exercised for every target so the
        # non-host branches are covered without a real Linux/Windows box.
        self.assertEqual(platformsupport._platform_from("posix", "darwin"), "macos")
        self.assertEqual(platformsupport._platform_from("nt", "win32"), "windows")
        self.assertEqual(platformsupport._platform_from("posix", "linux"), "linux")
        self.assertEqual(platformsupport._platform_from("posix", "linux2"), "linux")

        current = platformsupport.current_platform()
        self.assertIn(current, ("macos", "linux", "windows"))
        # Host mapping must match the runner OS (macOS locally, Linux in CI).
        expected = platformsupport._platform_from(os.name, __import__("sys").platform)
        self.assertEqual(current, expected)

    def test_is_posix_matches_os_name(self) -> None:
        self.assertEqual(platformsupport.is_posix(), os.name == "posix")
        self.assertTrue(platformsupport._is_posix_platform("macos"))
        self.assertTrue(platformsupport._is_posix_platform("linux"))
        self.assertFalse(platformsupport._is_posix_platform("windows"))

    def test_probed_platforms_is_macos_only(self) -> None:
        self.assertEqual(platformsupport.PROBED_PLATFORMS, ("macos",))


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.mkdtemp(prefix="grok-cli-platformsupport-perm-")
        self.addCleanup(shutil.rmtree, self.scratch, True)

    @unittest.skipUnless(platformsupport.is_posix(), "POSIX octal mode assertion")
    def test_restrict_permissions_posix_sets_0700_0600(self) -> None:
        directory = pathlib.Path(self.scratch) / "dir"
        directory.mkdir()
        os.chmod(str(directory), 0o777)
        platformsupport.restrict_dir_permissions(directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

        file_path = pathlib.Path(self.scratch) / "file"
        file_path.write_text("x", encoding="utf-8")
        os.chmod(str(file_path), 0o666)
        platformsupport.restrict_file_permissions(file_path)
        self.assertEqual(stat.S_IMODE(file_path.stat().st_mode), 0o600)

    def test_open_private_file_is_exclusive_and_restricted(self) -> None:
        target = pathlib.Path(self.scratch) / "private.bin"
        file_descriptor = platformsupport.open_private_file(target)
        try:
            os.write(file_descriptor, b"payload")
        finally:
            os.close(file_descriptor)

        self.assertTrue(target.is_file())
        # O_EXCL: a second create against the same path must be refused on
        # every platform, so an existing file is never silently adopted.
        with self.assertRaises(FileExistsError):
            platformsupport.open_private_file(target)

        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


class OwnershipTests(unittest.TestCase):
    def test_owning_uid_none_on_windows_branch(self) -> None:
        # The Windows branch never touches os.getuid (which does not exist on
        # Windows); it is exercised here on macOS via the injected platform.
        self.assertIsNone(platformsupport._owning_uid_for("windows"))
        posix_uid = platformsupport._owning_uid_for("macos")
        self.assertIsInstance(posix_uid, int)
        self.assertEqual(platformsupport.owning_uid_or_none(), os.getuid())

    def test_path_is_owned_by_current_user_branches(self) -> None:
        this_file = pathlib.Path(__file__)
        st = this_file.stat()
        # POSIX branch: uid-match against the real owner of this file.
        self.assertEqual(
            platformsupport._path_owned_for("macos", st),
            st.st_uid == os.getuid(),
        )
        # Windows branch: best-effort True (documented per-user profile temp
        # reliance), never a crash reading a meaningless st_uid.
        self.assertTrue(platformsupport._path_owned_for("windows", st))


class SpawnKwargsTests(unittest.TestCase):
    def test_spawn_kwargs_new_group_per_platform(self) -> None:
        posix_kwargs = platformsupport._spawn_kwargs_for("macos")
        self.assertEqual(posix_kwargs, {"start_new_session": True})

        windows_kwargs = platformsupport._spawn_kwargs_for("windows")
        self.assertIn("creationflags", windows_kwargs)
        # CREATE_NEW_PROCESS_GROUP == 0x00000200; resolved without importing a
        # Windows-only subprocess attribute that is absent on this host.
        self.assertEqual(windows_kwargs["creationflags"], 0x00000200)

        current = platformsupport.spawn_kwargs_new_group()
        if platformsupport.is_posix():
            self.assertEqual(current, {"start_new_session": True})
        else:
            self.assertIn("creationflags", current)


class KillProcessTreeTests(unittest.TestCase):
    @unittest.skipUnless(platformsupport.is_posix(), "POSIX killpg path")
    def test_kill_process_tree_posix_uses_killpg(self) -> None:
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **platformsupport.spawn_kwargs_new_group(),
        )
        try:
            with mock.patch("os.killpg") as mock_killpg:
                platformsupport.kill_process_tree(proc)
                mock_killpg.assert_called_once_with(os.getpgid(proc.pid), signal.SIGKILL)
        finally:
            # killpg was mocked out above, so the real child is still alive:
            # terminate the whole group for real, then reap it.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()


class RequireProbedPlatformTests(unittest.TestCase):
    def test_require_probed_platform_raises_on_unprobed(self) -> None:
        with mock.patch("groklib.platformsupport.current_platform", return_value="linux"):
            with self.assertRaises(GrokWrapperError) as caught:
                platformsupport.require_probed_platform_for_live()
        self.assertEqual(caught.exception.error_class, "probe-required")
        self.assertIn("linux", str(caught.exception))

        with mock.patch("groklib.platformsupport.current_platform", return_value="macos"):
            self.assertIsNone(platformsupport.require_probed_platform_for_live())


class ProcessIsAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(platformsupport.process_is_alive(os.getpid()))

    def test_reaped_child_is_not_alive(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(platformsupport.process_is_alive(child.pid))

    def test_non_positive_and_non_int_pids_are_not_alive(self) -> None:
        for pid in (0, -1):
            with self.subTest(pid=pid):
                self.assertFalse(platformsupport.process_is_alive(pid))

    def test_windows_reports_not_alive_without_calling_os_kill(self) -> None:
        # Windows never calls os.kill(0) (it would terminate the process); the
        # best-effort branch reports not-alive so age-based reaping still runs.
        with mock.patch("groklib.platformsupport.current_platform", return_value="windows"):
            with mock.patch("groklib.platformsupport.os.kill", side_effect=AssertionError("must not call os.kill")):
                self.assertFalse(platformsupport.process_is_alive(os.getpid()))


class WindowsAclPrincipalTests(unittest.TestCase):
    """Grok r3 #10: the icacls principal comes from the process token, never spoofable env."""

    def test_principal_resolved_from_whoami_not_username_env(self) -> None:
        fake_whoami = subprocess.CompletedProcess(args=["whoami"], returncode=0, stdout="CORP\\realuser\n", stderr="")
        with mock.patch("groklib.platformsupport.subprocess.run", return_value=fake_whoami) as run_mock:
            with mock.patch.dict(os.environ, {"USERNAME": "attacker", "USERDOMAIN": "EVIL"}):
                principal = platformsupport._current_windows_principal()
        self.assertEqual(principal, "CORP\\realuser")
        self.assertTrue(run_mock.called, "the principal must be read from the process token via whoami")

    def test_principal_is_none_when_whoami_fails(self) -> None:
        fail = subprocess.CompletedProcess(args=["whoami"], returncode=1, stdout="", stderr="")
        with mock.patch("groklib.platformsupport.subprocess.run", return_value=fail):
            self.assertIsNone(platformsupport._current_windows_principal())

    def test_acl_fails_closed_when_principal_unresolvable(self) -> None:
        # No resolvable token principal -> grant NOTHING (return False), relying on
        # the per-user profile temp ACL; never grant to a spoofed env principal.
        with mock.patch("groklib.platformsupport._current_windows_principal", return_value=None):
            self.assertFalse(platformsupport._restrict_via_windows_acl(pathlib.Path("C:/tmp/x"), is_dir=True))

    def test_acl_grant_uses_token_principal(self) -> None:
        captured = {}

        def fake_run(argv, **_kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch("groklib.platformsupport._current_windows_principal", return_value="CORP\\realuser"):
            with mock.patch("groklib.platformsupport.subprocess.run", side_effect=fake_run):
                ok = platformsupport._restrict_via_windows_acl(pathlib.Path("C:/tmp/x"), is_dir=True)
        self.assertTrue(ok)
        self.assertIn("CORP\\realuser:(OI)(CI)F", captured["argv"])


class ProcessStartTokenTests(unittest.TestCase):
    """F2/F4 pid-reuse identity: a per-process start-time token binds a liveness lease."""

    def test_self_token_is_stable_and_bogus_pids_are_none(self) -> None:
        token = platformsupport.process_start_token(os.getpid())
        if token is not None:
            # Two reads for the same live process must be identical.
            self.assertEqual(token, platformsupport.process_start_token(os.getpid()))
        for pid in (0, -1, None, True):
            with self.subTest(pid=pid):
                self.assertIsNone(platformsupport.process_start_token(pid))

    def test_windows_start_token_is_none(self) -> None:
        # Windows live modes are gated off, so no active home needs binding.
        self.assertIsNone(platformsupport._process_start_token_for("windows", os.getpid()))

    def test_macos_start_token_is_timezone_independent(self) -> None:
        # Round7 F1: the macOS `ps -o lstart=` token must render identically for the
        # SAME live process regardless of the reader's ambient TZ (the un-pinned
        # rendering varied by timezone, so the liveness lease's identity comparison
        # wrongly saw a live owner as recycled). The fix pins TZ=UTC/LC_ALL=C in the
        # subprocess env, so the ambient TZ can no longer change the token.
        if platformsupport.current_platform() != "macos":
            self.skipTest("macOS-only ps lstart normalization")
        pid = os.getpid()
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            los_angeles = platformsupport._process_start_token_for("macos", pid)
        with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
            tokyo = platformsupport._process_start_token_for("macos", pid)
        self.assertIsNotNone(los_angeles)
        self.assertEqual(los_angeles, tokyo)

    def test_macos_start_token_normalizes_whitespace(self) -> None:
        # The token collapses internal whitespace runs (ps pads the day-of-month
        # field, e.g. "Jul  7"), so a stable, parse-safe token is produced.
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Mon Jul  7 12:34:56 2026\n", stderr=""
        )
        with mock.patch("groklib.platformsupport.subprocess.run", return_value=fake):
            token = platformsupport._process_start_token_for("macos", 4321)
        self.assertEqual(token, "Mon Jul 7 12:34:56 2026")


if __name__ == "__main__":
    unittest.main()
