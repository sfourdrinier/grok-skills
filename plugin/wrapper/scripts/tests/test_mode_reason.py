# wrapper/scripts/tests/test_mode_reason.py

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from groklib import GrokWrapperError, envelope as envelope_mod
from groklib import platformsupport, runstate
from groklib.modes import reason

from tests.modefixtures import ModeHarness


class ReasonModeTests(ModeHarness):
    """reason runs Grok in an isolated temp cwd with only explicitly-supplied artifacts and rules."""

    def _make_rules_repo(self) -> pathlib.Path:
        # A real git repo: reason derives the repo root (for labeling --rules-file
        # blocks with their repo-relative path) from the caller's cwd git toplevel,
        # so the fixture must be a genuine checkout, like production.
        repo = pathlib.Path(self.tmp_root) / "rules-repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "notes.md").write_text("# Selected rules\n\nBe rigorous.\n", encoding="utf-8")
        for args in (
            ["init", "-q"],
            ["config", "user.name", "Grok CLI Test"],
            ["config", "user.email", "grok-cli-test@example.com"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "rules repo"],
        ):
            subprocess.run(["git", "-C", str(repo)] + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return repo

    def test_reason_cwd_outside_repo(self) -> None:
        exit_code, out = self.drive(["reason", "--task", "Give a cold second opinion"])
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        cwd = pathlib.Path(self.flag_value(argv, "--cwd")).resolve()
        temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
        self.assertTrue(str(cwd).startswith(str(temp_root)), "reason cwd must live under the OS temp dir")

    def test_reason_no_rules_discovery_prompt_has_no_rules_block_without_rules_file(self) -> None:
        exit_code, out = self.drive(["reason", "--task", "Compare these two designs"])
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        prompt = self.read_prompt(argv)
        self.assertNotIn("=== REPOSITORY RULES", prompt)
        self.assertIn("Compare these two designs", prompt)

    def test_reason_inputs_copied_read_only_and_named_in_prompt(self) -> None:
        # Part A: the copy helper is a directly-testable unit (read-only property).
        cwd = pathlib.Path(self.tmp_root) / "reason-cwd-unit"
        cwd.mkdir()
        source = pathlib.Path(self.tmp_root) / "artifact.txt"
        source.write_text("artifact body\n", encoding="utf-8")
        names = reason._copy_input_files(cwd, [str(source)])
        self.assertEqual(names, ["artifact.txt"])
        copied = cwd / "artifact.txt"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_text(encoding="utf-8"), "artifact body\n")
        if platformsupport.is_posix():
            self.assertEqual(stat.S_IMODE(os.stat(str(copied)).st_mode), 0o400)

        # Part B: end-to-end, the input is named in the prompt and read_file is enabled.
        input_path = pathlib.Path(self.tmp_root) / "diff-to-review.txt"
        input_path.write_text("a supplied diff\n", encoding="utf-8")
        exit_code, out = self.drive(
            [
                "reason",
                "--no-web",
                "--task",
                "Critique the supplied diff",
                "--input",
                str(input_path),
            ]
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        prompt = self.read_prompt(argv)
        self.assertIn("diff-to-review.txt", prompt)
        self.assertEqual(self.flag_value(argv, "--tools"), "read_file")

    def test_reason_rules_file_blocks_use_c7_format(self) -> None:
        repo = self._make_rules_repo()
        rules_file = repo / "pkg" / "notes.md"
        exit_code, out = self.drive(
            ["reason", "--task", "Reason about this", "--rules-file", str(rules_file)],
            repo_root=repo,
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        prompt = self.read_prompt(argv)
        self.assertIn("=== REPOSITORY RULES (governing; read completely before the task) ===", prompt)
        self.assertIn("--- BEGIN pkg/notes.md ---", prompt)
        self.assertIn("--- END pkg/notes.md ---", prompt)
        self.assertIn("Be rigorous.", prompt)
        envelope = json.loads(out)
        self.assertEqual(envelope["instructions"][0]["path"], "pkg/notes.md")

    def test_reason_tools_empty_without_inputs(self) -> None:
        exit_code, out = self.drive(
            ["reason", "--no-web", "--task", "Pure reasoning, no files"]
        )
        self.assertEqual(exit_code, 0, out)
        argv = self.read_run_argv()
        self.assertNotIn("--tools", argv)
        self.assertIn("--disallowed-tools", argv)

    def test_reason_defaults_web_off(self) -> None:
        exit_code, out = self.drive(["reason", "--task", "Pure reasoning, no files"])
        self.assertEqual(exit_code, 0, out)
        envelope = json.loads(out)
        self.assertFalse(envelope["policy"]["webAccess"])

    def test_reason_web_flag_enables_web(self) -> None:
        exit_code, out = self.drive(["reason", "--web", "--task", "Pure reasoning, no files"])
        self.assertEqual(exit_code, 0, out)
        envelope = json.loads(out)
        self.assertTrue(envelope["policy"]["webAccess"])
        argv = self.read_run_argv()
        tools = self.flag_value(argv, "--tools")
        self.assertIn("web_search", tools)

    def test_reason_effective_model_mismatch_fails(self) -> None:
        exit_code, out = self.drive(
            ["reason", "--task", "Reason"], scenario="model-mismatch"
        )
        envelope = json.loads(out)
        self.assertEqual(exit_code, 1)
        self.assertEqual(envelope["status"], "failure")
        self.assertEqual(envelope["error"]["class"], "model-unavailable")

    def test_reason_cwd_removed_on_create_run_collision(self) -> None:
        # F4: a create_run collision raises before run_grok_mode's own
        # extra_temp_dirs cleanup is armed. The reason cwd (holding the copied
        # --input artifacts) must NOT leak on that path.
        input_path = pathlib.Path(self.tmp_root) / "artifact.txt"
        input_path.write_text("supplied artifact body\n", encoding="utf-8")
        temp_root = pathlib.Path(tempfile.gettempdir())
        before = set(temp_root.glob("grok-reason-cwd-*"))

        def _boom(mode):
            raise GrokWrapperError(
                "state-ownership-violation", "simulated create_run collision", {"mode": mode}
            )

        with mock.patch.object(runstate, "create_run", _boom):
            exit_code, out = self.drive(
                ["reason", "--task", "Critique the diff", "--input", str(input_path)]
            )
        env = json.loads(out)
        self.assertEqual(exit_code, 1, out)
        self.assertEqual(env["status"], "failure")
        after = set(temp_root.glob("grok-reason-cwd-*"))
        self.assertEqual(after - before, set(), "reason cwd must not leak on a create_run collision")

    def test_reason_success_envelope_validates(self) -> None:
        exit_code, out = self.drive(["reason", "--task", "Reason"])
        envelope = json.loads(out)
        self.assertEqual(exit_code, 0, out)
        self.assertEqual(envelope_mod.validate_envelope(envelope), [])
        self.assertEqual(envelope["mode"], "reason")
        self.assertIsNotNone(envelope["progressStreamPath"])


if __name__ == "__main__":
    unittest.main()
