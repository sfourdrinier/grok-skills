# wrapper/scripts/tests/probedplatform.py
#
# Unit tests exercise sandbox verification and live-mode lifecycles that require
# a probed platform (macOS only in production). CI runs on Linux; patch
# current_platform to "macos" so tests validate sandbox logic rather than the
# platform gate. Production code is unchanged: real Linux hosts still get
# probe-required.

from unittest import mock

from groklib import platformsupport


class ProbedPlatformMixin:
    """Treat the host as probed macOS for the duration of each test."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(platformsupport, "current_platform", return_value="macos")
        patcher.start()
        self.addCleanup(patcher.stop)
