# wrapper/scripts/tests/preflightfixtures.py
#
# Shared harness for the preflight-driving tests (test_mode_preflight and the
# entrypoint success/stored-write paths). It isolates XDG_STATE_HOME, points
# GROK_AGENT_BINARY at the fake grok CLI, and seeds a fake source ~/.grok with
# an auth.json. Temp-dir isolation comes from TempHomeIsolationMixin: each test
# redirects tempfile.tempdir into its own "gsi-" directory under the real
# $TMPDIR, so the global "gs-*" private-home scan sees only this test's homes.
# The isolated dir sits under the real /var/folders/... root, so the
# leader-socket path stays realistic (~93 bytes), still well under
# runstate.allocate_leader_socket's 100-byte AF_UNIX guard.
# run_preflight() drives grok_agent.main
# end-to-end while injecting the fake CLI scenario into each private home the
# mode creates (the fake reads its scenario from <HOME>/fake-grok-control.json,
# and the private home is minted inside the mode, so the only clean seam is a
# thin wrapper around authhome.create_private_home that drops the control file
# after the real home is built).

import contextlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import grok_agent
from groklib.authhome import create_private_home

from tests.probedplatform import ProbedPlatformMixin
from tests.temphomeisolation import TempHomeIsolationMixin

_FAKE_BINARY = pathlib.Path(__file__).resolve().parent / "fake_grok.py"


class PreflightHarness(ProbedPlatformMixin, TempHomeIsolationMixin, unittest.TestCase):
    """Fully-isolated environment for driving grok_agent.main(["preflight"])."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-preflight-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)

        self.state_home = os.path.join(self.tmp_root, "state-home")
        os.makedirs(self.state_home, exist_ok=True)
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"XDG_STATE_HOME": self.state_home, "GROK_AGENT_BINARY": str(_FAKE_BINARY)},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        self.grok_home = pathlib.Path(self.tmp_root) / "grok-home" / ".grok"
        self.grok_home.mkdir(parents=True)
        (self.grok_home / "auth.json").write_text("{}\n", encoding="utf-8")

    def empty_grok_dir(self) -> pathlib.Path:
        empty = pathlib.Path(self.tmp_root) / "empty-grok"
        empty.mkdir()
        return empty

    def run_preflight(self, scenarios=("models-ok", "inspect-ok"), source_grok_dir=None):
        """Drive main(["preflight"]) and return (exit_code, stdout_text).

        ``scenarios`` are written, in order, into the fake control file of each
        private home the mode creates (first = login home, second = inspect
        home). ``source_grok_dir`` overrides the fake ~/.grok used for the
        auth-material and private-home checks.
        """
        from groklib.modes import preflight

        src = source_grok_dir if source_grok_dir is not None else self.grok_home
        state = {"n": 0}

        def _patched_create(**kwargs):
            home = create_private_home(**kwargs)
            index = state["n"]
            scenario = scenarios[index] if index < len(scenarios) else scenarios[-1]
            state["n"] = index + 1
            (home.home_dir / "fake-grok-control.json").write_text(
                json.dumps({"scenario": scenario}), encoding="utf-8"
            )
            return home

        buffer = io.StringIO()
        with mock.patch.object(preflight, "_source_grok_dir", lambda: pathlib.Path(src)):
            with mock.patch.object(preflight, "create_private_home", _patched_create):
                with contextlib.redirect_stdout(buffer):
                    exit_code = grok_agent.main(["preflight"])
        return exit_code, buffer.getvalue()
