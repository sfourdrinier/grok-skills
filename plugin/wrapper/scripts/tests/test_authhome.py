# wrapper/scripts/tests/test_authhome.py

import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from groklib import GrokWrapperError, authhome, platformsupport, runstate
from groklib.authhome import PrivateHome

from tests.temphomeisolation import TempHomeIsolationMixin


class AuthHomeTests(TempHomeIsolationMixin, unittest.TestCase):
    """Covers create_private_home/destroy_private_home/render_config_toml (C2 private-home isolation)."""

    def setUp(self) -> None:
        super().setUp()
        self.scratch_dir = tempfile.mkdtemp(prefix="grok-cli-authhome-test-")
        self.addCleanup(shutil.rmtree, self.scratch_dir, True)
        self.source_grok_dir = pathlib.Path(self.scratch_dir) / "source-grok"
        self.source_grok_dir.mkdir()

    def _write_source_auth_file(self, name: str, content: bytes) -> pathlib.Path:
        path = self.source_grok_dir / name
        path.write_bytes(content)
        os.chmod(path, 0o600)
        return path

    def _cleanup_home(self, home: PrivateHome) -> None:
        if home.home_dir.exists():
            shutil.rmtree(str(home.home_dir), ignore_errors=True)

    def test_create_copies_auth_with_0600_and_home_0700(self) -> None:
        self._write_source_auth_file("auth.json", b"AUTH-FILE-CONTENT")
        config_toml = "placeholder config\n"

        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml=config_toml,
        )
        self.addCleanup(self._cleanup_home, home)

        # POSIX-octal permission modes (D-PORT): home.home_dir/home.grok_dir
        # have no existence check yet at this point, so is_dir() is added
        # here as the platform-neutral floor before the POSIX-only 0o700
        # mode check runs.
        self.assertTrue(home.home_dir.is_dir())
        self.assertTrue(home.grok_dir.is_dir())
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(home.home_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(home.grok_dir.stat().st_mode), 0o700)
        self.assertEqual(home.grok_dir, home.home_dir / ".grok")

        copied_auth_path = home.grok_dir / "auth.json"
        self.assertTrue(copied_auth_path.is_file())
        # POSIX-octal permission mode (D-PORT): copied_auth_path.is_file()
        # above already gives platform-neutral existence/type coverage;
        # only the exact 0o600 mode check is POSIX-only.
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(copied_auth_path.stat().st_mode), 0o600)
        self.assertEqual(copied_auth_path.read_bytes(), b"AUTH-FILE-CONTENT")

        self.assertEqual(home.config_path, home.grok_dir / "config.toml")
        self.assertTrue(home.config_path.is_file())
        # POSIX-octal permission mode (D-PORT): home.config_path.is_file()
        # above already gives platform-neutral existence/type coverage;
        # only the exact 0o600 mode check is POSIX-only.
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(home.config_path.stat().st_mode), 0o600)
        self.assertEqual(home.config_path.read_text(encoding="utf-8"), config_toml)

        # Grok dogfood-3 #1: the private home carries a liveness lease naming the
        # owning wrapper process (this test process), so the reaper never deletes
        # an active home by age alone.
        liveness_path = home.home_dir / runstate.TEMP_HOME_LIVENESS_FILENAME
        self.assertTrue(liveness_path.is_file())
        lease = json.loads(liveness_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["pid"], os.getpid())

    def test_create_private_home_multiple_auth_files_all_copied(self) -> None:
        # Extra coverage beyond the brief's named tests: auth_file_names is a
        # tuple per the interface, not a single name; every present file
        # must be copied and independently chmod'd.
        self._write_source_auth_file("auth.json", b"AUTH-A")
        self._write_source_auth_file("other-auth.json", b"AUTH-B")

        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json", "other-auth.json"),
            config_toml="cfg\n",
        )
        self.addCleanup(self._cleanup_home, home)

        for name, content in (("auth.json", b"AUTH-A"), ("other-auth.json", b"AUTH-B")):
            copied_path = home.grok_dir / name
            self.assertTrue(copied_path.is_file())
            # POSIX-octal permission mode (D-PORT): copied_path.is_file()
            # above already gives platform-neutral existence/type coverage;
            # only the exact 0o600 mode check is POSIX-only.
            if platformsupport.is_posix():
                self.assertEqual(stat.S_IMODE(copied_path.stat().st_mode), 0o600)
            self.assertEqual(copied_path.read_bytes(), content)

    def test_missing_auth_file_fails_closed_as_auth_missing(self) -> None:
        # source_grok_dir intentionally has no auth.json. The check must
        # happen BEFORE any temp directory is created (zero filesystem side
        # effects on this failure path).
        with mock.patch("groklib.authhome.tempfile.mkdtemp") as mock_mkdtemp:
            with self.assertRaises(GrokWrapperError) as ctx:
                authhome.create_private_home(
                    source_grok_dir=self.source_grok_dir,
                    auth_file_names=("auth.json",),
                    config_toml="cfg\n",
                )
            mock_mkdtemp.assert_not_called()

        self.assertEqual(ctx.exception.error_class, "auth-missing")
        self.assertIn("auth.json", ctx.exception.detail.get("missingAuthFileNames", []))

    def test_create_writes_sandbox_toml_only_when_provided(self) -> None:
        self._write_source_auth_file("auth.json", b"AUTH-CONTENT")

        home_without_sandbox = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(self._cleanup_home, home_without_sandbox)
        self.assertFalse((home_without_sandbox.grok_dir / "sandbox.toml").exists())

        home_with_sandbox = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
            sandbox_toml="[profiles.workspace]\n",
        )
        self.addCleanup(self._cleanup_home, home_with_sandbox)
        sandbox_path = home_with_sandbox.grok_dir / "sandbox.toml"
        self.assertTrue(sandbox_path.is_file())
        # POSIX-octal permission mode (D-PORT): sandbox_path.is_file()
        # above already gives platform-neutral existence/type coverage;
        # only the exact 0o600 mode check is POSIX-only.
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(sandbox_path.stat().st_mode), 0o600)
        self.assertEqual(sandbox_path.read_text(encoding="utf-8"), "[profiles.workspace]\n")

    def test_config_toml_written_and_never_contains_always_approve_true(self) -> None:
        rendered = authhome.render_config_toml(mode="review")

        self.assertIn('permission_mode = "auto"', rendered)
        self.assertIn("enabled = false", rendered)
        self.assertNotIn("always-approve", rendered)
        self.assertNotIn("= true", rendered)

        self._write_source_auth_file("auth.json", b"AUTH-CONTENT")
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml=rendered,
        )
        self.addCleanup(self._cleanup_home, home)

        on_disk = home.config_path.read_text(encoding="utf-8")
        self.assertEqual(on_disk, rendered)
        self.assertNotIn("always-approve", on_disk)
        self.assertNotIn("= true", on_disk)

    def test_render_config_toml_rejects_empty_mode(self) -> None:
        with self.assertRaises(GrokWrapperError) as ctx:
            authhome.render_config_toml(mode="")
        self.assertEqual(ctx.exception.error_class, "usage-error")

    def test_render_config_toml_rejects_non_lowercase_mode(self) -> None:
        # A mode carrying a newline plus a fake TOML line must be rejected
        # BEFORE it is ever interpolated into the rendered config, and the
        # raised exception's message must not carry the raw newline or the
        # injected line verbatim (only a repr(), truncated to 40 chars).
        injected_mode = 'review\npermission_mode = "bypassPermissions"'

        with self.assertRaises(ValueError) as ctx:
            authhome.render_config_toml(mode=injected_mode)
        exception_message = str(ctx.exception)
        self.assertNotIn("\n", exception_message)
        self.assertNotIn(injected_mode, exception_message)
        # Only a repr(), truncated to 40 chars, of the rejected value may
        # appear: the raw newline-carrying string must never be
        # interpolated unsanitized.
        self.assertIn(repr(injected_mode)[:40], exception_message)

        # Uppercase characters are also rejected by the same ^[a-z]+$ gate.
        with self.assertRaises(ValueError):
            authhome.render_config_toml(mode="Review")

        # Empty mode continues to be rejected by the pre-existing
        # isinstance/non-empty check (GrokWrapperError), not the new regex
        # gate; this test also covers that case per the review brief.
        with self.assertRaises(GrokWrapperError) as empty_ctx:
            authhome.render_config_toml(mode="")
        self.assertEqual(empty_ctx.exception.error_class, "usage-error")

    def test_destroy_removes_auth_material_first_and_reports_clean(self) -> None:
        # Happy path: nothing fails, everything is removed, status "clean".
        self._write_source_auth_file("auth.json", b"AUTH-CONTENT")
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )

        result = authhome.destroy_private_home(home)

        self.assertEqual(result, {"status": "clean", "detail": None})
        self.assertFalse(home.home_dir.exists())

        # Failure-injection sub-scenario: a non-auth file's (config.toml)
        # removal fails once, simulated by patching os.remove to raise the
        # first time it is called with that basename. The auth copy must
        # still be removed first and confirmed absent, even though the
        # overall status is reported "failed" because of the non-auth
        # residue.
        self._write_source_auth_file("auth.json", b"AUTH-CONTENT-2")
        home2 = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(lambda: shutil.rmtree(str(home2.home_dir), ignore_errors=True))
        auth_copy_path = home2.grok_dir / "auth.json"
        self.assertTrue(auth_copy_path.is_file())

        real_remove = os.remove
        call_state = {"config_toml_calls": 0}

        def flaky_remove(path: str) -> None:
            if os.path.basename(str(path)) == "config.toml" and call_state["config_toml_calls"] == 0:
                call_state["config_toml_calls"] += 1
                raise OSError("simulated removal failure for a non-auth file")
            real_remove(path)

        with mock.patch("os.remove", side_effect=flaky_remove):
            result2 = authhome.destroy_private_home(home2)

        self.assertEqual(result2["status"], "failed")
        self.assertFalse(auth_copy_path.exists())

    def test_destroy_classifies_nested_grok_credential_as_auth_material(self) -> None:
        # Grok r3 #13 nested-grok-auth-classification: a credential file Grok wrote
        # into a NESTED subdir under .grok/ is auth material too. If its removal
        # fails, the outcome is the AUTH-specific failed detail (path-free), not the
        # weaker "residual non-auth" classification.
        self._write_source_auth_file("auth.json", b"AUTH-CONTENT")
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(lambda: shutil.rmtree(str(home.home_dir), ignore_errors=True))

        nested_dir = home.grok_dir / "sessions"
        nested_dir.mkdir()
        nested_cred = nested_dir / "token.json"
        nested_cred.write_bytes(b"NESTED-CREDENTIAL")

        real_remove = os.remove

        def fail_nested_cred(path: str) -> None:
            if os.path.basename(str(path)) == "token.json":
                raise OSError("simulated nested credential removal failure")
            real_remove(path)

        with mock.patch("os.remove", side_effect=fail_nested_cred):
            result = authhome.destroy_private_home(home)

        self.assertEqual(result["status"], "failed")
        # The AUTH-specific, path-free detail -- not the residual-non-auth detail.
        self.assertIn("authentication material", result["detail"])
        self.assertNotIn(os.sep, result["detail"])

    def test_destroy_detail_never_contains_home_path_when_auth_removal_failed(self) -> None:
        self._write_source_auth_file("auth.json", b"AUTH-CONTENT")
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(lambda: shutil.rmtree(str(home.home_dir), ignore_errors=True))

        real_remove = os.remove

        def always_fail_for_auth(path: str) -> None:
            if os.path.basename(str(path)) == "auth.json":
                raise OSError("simulated persistent auth removal failure")
            real_remove(path)

        with mock.patch("os.remove", side_effect=always_fail_for_auth):
            result = authhome.destroy_private_home(home)

        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(result["detail"])
        self.assertNotIn(str(home.home_dir), result["detail"])
        self.assertNotIn(str(home.grok_dir), result["detail"])
        self.assertNotIn(os.sep, result["detail"])

    def test_create_failure_non_oserror_never_leaks_auth_copy(self) -> None:
        # A non-OSError raised AFTER the auth copy loop (simulated here by
        # patching the config-write helper, which runs right after the
        # auth-file copy loop completes) must still trigger the
        # best-effort partial-home cleanup. Before the any-exception fix,
        # the cleanup guard was `except OSError`, so a ValueError here
        # would propagate straight past cleanup and leave the copied
        # sentinel auth material on disk.
        sentinel = b"NON-OSERROR-CLEANUP-SENTINEL"
        self._write_source_auth_file("auth.json", sentinel)

        real_mkdtemp = tempfile.mkdtemp
        created_paths = []

        def recording_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created_paths.append(path)
            return path

        real_os_write = os.write
        captured_stderr_writes = []

        def capturing_os_write(file_descriptor, data):
            if file_descriptor == 2:
                captured_stderr_writes.append(data)
            return real_os_write(file_descriptor, data)

        with mock.patch("groklib.authhome.tempfile.mkdtemp", side_effect=recording_mkdtemp):
            with mock.patch(
                "groklib.authhome._write_text_0600",
                side_effect=ValueError("simulated post-copy failure"),
            ):
                with mock.patch("os.write", side_effect=capturing_os_write):
                    with self.assertRaises(ValueError) as ctx:
                        authhome.create_private_home(
                            source_grok_dir=self.source_grok_dir,
                            auth_file_names=("auth.json",),
                            config_toml="cfg\n",
                        )

        # Exactly one private home was ever created for this call, and it
        # must no longer exist after the ValueError propagated: the
        # cleanup guard removed it (auth copy included) before re-raising.
        self.assertEqual(len(created_paths), 1)
        home_dir = pathlib.Path(created_paths[0])
        self.assertTrue(home_dir.name.startswith(authhome.TEMP_HOME_PREFIX))
        self.assertFalse(home_dir.exists())
        self.assertFalse((home_dir / ".grok" / "auth.json").exists())

        # The exception is the original ValueError, propagated unchanged,
        # and the sentinel auth content never appears in its message.
        sentinel_text = sentinel.decode("ascii")
        self.assertNotIn(sentinel_text, str(ctx.exception))

        # Nor does the sentinel appear anywhere in what was logged to
        # stderr while building/discarding the partial home.
        stderr_text = b"".join(captured_stderr_writes).decode("utf-8", errors="replace")
        self.assertNotIn(sentinel_text, stderr_text)

    def test_no_function_ever_reads_auth_bytes_into_returned_values(self) -> None:
        sentinel = b"SECRET-SENTINEL"
        self._write_source_auth_file("auth.json", sentinel)

        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.assertNotIn("SECRET-SENTINEL", repr(home))

        result = authhome.destroy_private_home(home)
        self.assertNotIn("SECRET-SENTINEL", repr(result))

    def test_fresh_private_home_carries_a_verifiable_owner_marker(self) -> None:
        # Round-3 (Grok dogfood #1): a private home MUST carry an owner.json the
        # instant it is built, or runstate.audit_stale_temp_homes can never reap
        # a crashed home and its live auth copy accumulates on disk forever.
        self._write_source_auth_file("auth.json", b"AUTH-FILE-CONTENT")
        home = authhome.create_private_home(
            source_grok_dir=self.source_grok_dir,
            auth_file_names=("auth.json",),
            config_toml="cfg\n",
        )
        self.addCleanup(self._cleanup_home, home)

        marker_path = home.home_dir / "owner.json"
        self.assertTrue(marker_path.is_file())
        # verify_owner_marker returns the marker run id and raises on any
        # missing/malformed/foreign marker; a fresh private home must verify.
        marker_run_id = runstate.verify_owner_marker(marker_path)
        self.assertTrue(runstate.is_valid_run_id(marker_run_id))
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)

    def test_audit_reaps_stale_marked_private_home_but_not_foreign_dirs(self) -> None:
        # The marker written at creation makes a crashed (never-destroyed) home
        # reapable by the stale-home audit, while a foreign gs-* dir with no
        # verifiable marker is left untouched.
        self._write_source_auth_file("auth.json", b"AUTH-FILE-CONTENT")
        fake_temp_root = tempfile.mkdtemp(prefix="grok-cli-authhome-audit-")
        self.addCleanup(shutil.rmtree, fake_temp_root, True)
        old_ts = 0.0  # 1970-01-01: older than any max_age_seconds we pass

        with mock.patch("tempfile.gettempdir", return_value=fake_temp_root):
            home = authhome.create_private_home(
                source_grok_dir=self.source_grok_dir,
                auth_file_names=("auth.json",),
                config_toml="cfg\n",
            )
            # Simulate a crash: the home persists (no destroy), ages out, AND its
            # owning process is gone. create_private_home stamped the live test pid
            # into the liveness lease, so overwrite it with a genuinely-dead pid --
            # otherwise the reaper correctly protects it as an active home
            # (Grok dogfood-3 #1).
            import subprocess
            import sys

            child = subprocess.Popen([sys.executable, "-c", "pass"])
            child.wait()
            runstate.write_home_liveness_marker(home.home_dir, child.pid)
            os.utime(home.home_dir, (old_ts, old_ts))

            # A foreign gs-* dir with NO owner marker must never be reaped.
            foreign = pathlib.Path(fake_temp_root) / (authhome.TEMP_HOME_PREFIX + "foreign")
            foreign.mkdir(mode=0o700)
            os.chmod(foreign, 0o700)
            os.utime(foreign, (old_ts, old_ts))

            removed = runstate.audit_stale_temp_homes(max_age_seconds=3600)

        self.assertIn(str(home.home_dir), removed)
        self.assertFalse(home.home_dir.exists())
        self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
