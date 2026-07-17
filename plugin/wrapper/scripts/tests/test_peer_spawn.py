# wrapper/scripts/tests/test_peer_spawn.py
#
# _spawn_acp_child env/argv posture (PR #5 review B1/B2): the long-lived ACP
# child must run on the minimal C6 env (no operator-credential passthrough) and
# under the same global --sandbox <profile> confinement as code mode.

import pathlib
import shutil
import tempfile
import types
import unittest
from unittest import mock

from groklib.modes import peer as peer_mod


class _FakeProc:
    pid = 4321


class SpawnAcpChildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="spawn-acp-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _spawn(self, policy):
        home = types.SimpleNamespace(home_dir=pathlib.Path(self.tmp) / "home")
        home.home_dir.mkdir()
        worktree = types.SimpleNamespace(path=pathlib.Path(self.tmp) / "wt")
        worktree.path.mkdir()
        captured = {}

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc()

        with mock.patch.object(peer_mod.subprocess, "Popen", _fake_popen), mock.patch.object(
            peer_mod.platformsupport, "spawn_kwargs_new_group", return_value={}
        ):
            peer_mod._spawn_acp_child(
                binary=pathlib.Path("/usr/bin/true"),
                home=home,
                worktree=worktree,
                leader_socket=pathlib.Path(self.tmp) / "s.sock",
                model="grok-4.5",
                policy=policy,
            )
        return captured

    def test_global_sandbox_flag_precedes_agent(self) -> None:
        cap = self._spawn(types.SimpleNamespace(profile="grok-skills-peer"))
        argv = cap["argv"]
        self.assertIn("--sandbox", argv)
        self.assertIn("agent", argv)
        self.assertLess(
            argv.index("--sandbox"), argv.index("agent"),
            "global --sandbox must precede the agent subcommand",
        )
        self.assertEqual(argv[argv.index("--sandbox") + 1], "grok-skills-peer")
        self.assertEqual(argv[-2:], ["--leader-socket", str(pathlib.Path(self.tmp) / "s.sock")])

    def test_minimal_env_has_no_operator_credential_leak(self) -> None:
        cap = self._spawn(types.SimpleNamespace(profile="grok-skills-peer"))
        env = cap["env"]
        # Exactly the C6 minimal env: HOME/PATH/TMPDIR, nothing from os.environ.
        self.assertEqual(set(env.keys()), {"HOME", "PATH", "TMPDIR"})
        for leaked in ("DATABASE_URL", "SSH_AUTH_SOCK", "GOOGLE_APPLICATION_CREDENTIALS", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(leaked, env)

    def test_no_sandbox_flag_when_policy_has_no_profile(self) -> None:
        cap = self._spawn(types.SimpleNamespace(profile=None))
        self.assertNotIn("--sandbox", cap["argv"])


class KillRecordedChildSafetyTests(unittest.TestCase):
    """_kill_recorded_child must fail safe: never SIGKILL an unconfirmed or
    same-group pid (a killpg would take down the wrapper / test runner)."""

    @staticmethod
    def _doc(pid, token):
        return {"child": {"pid": pid, "startToken": token}}

    def test_no_kill_without_token(self):
        with mock.patch.object(peer_mod.platformsupport, "kill_process_tree_by_pid") as k:
            peer_mod._kill_recorded_child(self._doc(12345, None))
            k.assert_not_called()

    def test_no_kill_on_token_mismatch(self):
        with mock.patch.multiple(
            peer_mod.platformsupport,
            process_is_alive=mock.Mock(return_value=True),
            process_start_token=mock.Mock(return_value="other"),
            is_posix=mock.Mock(return_value=True),
            kill_process_tree_by_pid=mock.DEFAULT,
        ) as m:
            peer_mod._kill_recorded_child(self._doc(12345, "mine"))
            m["kill_process_tree_by_pid"].assert_not_called()

    def test_no_kill_when_pid_shares_our_group(self):
        with mock.patch.multiple(
            peer_mod.platformsupport,
            process_is_alive=mock.Mock(return_value=True),
            process_start_token=mock.Mock(return_value="mine"),
            is_posix=mock.Mock(return_value=True),
            kill_process_tree_by_pid=mock.DEFAULT,
        ) as m, mock.patch.object(peer_mod.os, "getpgid", return_value=999):
            peer_mod._kill_recorded_child(self._doc(12345, "mine"))
            m["kill_process_tree_by_pid"].assert_not_called()

    def test_kills_confirmed_child_in_other_group(self):
        with mock.patch.multiple(
            peer_mod.platformsupport,
            process_is_alive=mock.Mock(return_value=True),
            process_start_token=mock.Mock(return_value="mine"),
            is_posix=mock.Mock(return_value=True),
            kill_process_tree_by_pid=mock.DEFAULT,
        ) as m, mock.patch.object(
            peer_mod.os, "getpgid", side_effect=lambda pid: 111 if pid == 12345 else 222
        ):
            peer_mod._kill_recorded_child(self._doc(12345, "mine"))
            m["kill_process_tree_by_pid"].assert_called_once_with(12345)


if __name__ == "__main__":
    unittest.main()
