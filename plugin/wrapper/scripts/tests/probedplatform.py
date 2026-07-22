# wrapper/scripts/tests/probedplatform.py
#
# Unit tests exercise sandbox verification and live-mode lifecycles that require
# a probed platform. Fake Grok fixtures emit macos/seatbelt telemetry, so tests
# pin current_platform to "macos" for a stable expected_sandbox_platform match
# on both macOS and Linux CI runners. Production Linux hosts are probed
# (linux/landlock) as of 2.0.1; this mixin does not change production gates.

from unittest import mock

from groklib import platformsupport


class ProbedPlatformMixin:
    """Treat the host as probed macOS for the duration of each test (stable fakes)."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(platformsupport, "current_platform", return_value="macos")
        patcher.start()
        self.addCleanup(patcher.stop)
