# wrapper/scripts/tests/temphomeisolation.py
#
# Shared setUp/tearDown mixin that isolates the OS temp directory per test so
# the global "gs-*" private-home scan (runstate.audit_stale_temp_homes and the
# test-side temp_home_prefix_dirs helper) sees ONLY the homes this one test
# created. Without it, every test that mints a private home does so under the
# single shared real $TMPDIR, and one test's home (or a leaked home) pollutes
# another test's teardown/no-leak assertions -- each test passes alone but the
# full "discover" run flakes order-dependently.
#
# create_private_home mints homes via tempfile.mkdtemp(prefix=TEMP_HOME_PREFIX)
# with no dir= argument, so it resolves the temp root through
# tempfile.gettempdir() at call time. Redirecting tempfile.tempdir (and TMPDIR/
# TMP/TEMP) therefore steers both the runtime mint and the test-side scan into
# this test's private directory with no runtime-code change.
#
# The isolated directory is created UNDER the real system temp root with a short
# "gsi-" prefix (which does NOT match the "gs-" home scan), so the resulting
# leader-socket path stays realistic (~93 bytes on macOS /var/folders/...), well
# under runstate.allocate_leader_socket's 100-byte AF_UNIX guard. A regression
# that pushed the socket path over the limit would still be caught here.

import os
import shutil
import tempfile


class TempHomeIsolationMixin:
    """Redirect tempfile + TMPDIR to a fresh per-test directory, restored on cleanup.

    Subclasses MUST call ``super().setUp()`` as the FIRST statement of their own
    ``setUp`` so the redirect is in effect before they mint any temp files or
    private homes.
    """

    _TEMP_ENV_KEYS = ("TMPDIR", "TMP", "TEMP")

    def setUp(self) -> None:
        # Ambient TMPDIR may already be nested (agent sandboxes, CI wrappers).
        # Budget ~50 bytes of root so gsi-*/gs-*/.grok/l-*.sock stays under the
        # 100-byte AF_UNIX guard in runstate.allocate_leader_socket.
        real_temp_root = tempfile.gettempdir()
        if len(os.fsencode(real_temp_root)) > 50:
            for candidate in ("/tmp", "/private/tmp"):
                if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                    real_temp_root = candidate
                    break
        isolated_dir = tempfile.mkdtemp(prefix="gsi-", dir=real_temp_root)

        saved_tempdir = tempfile.tempdir
        saved_env = {key: os.environ.get(key) for key in self._TEMP_ENV_KEYS}

        def _restore() -> None:
            tempfile.tempdir = saved_tempdir
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(isolated_dir, ignore_errors=True)

        # Registered BEFORE the redirect so it runs LAST (addCleanup is LIFO):
        # every subclass cleanup that removes state under the isolated dir runs
        # first, then this restores tempfile.tempdir and removes the dir itself.
        self.addCleanup(_restore)

        tempfile.tempdir = isolated_dir
        for key in self._TEMP_ENV_KEYS:
            os.environ[key] = isolated_dir
        self.temp_home_isolated_dir = isolated_dir

        super().setUp()
