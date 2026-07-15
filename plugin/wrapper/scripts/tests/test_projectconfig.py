# wrapper/scripts/tests/test_projectconfig.py
#
# Covers the repo-agnostic ProjectConfig: package-manager auto-detection from
# the target repo's lockfile, the zero-config defaults, the optional
# .grok-skills.json overrides, and the fail-closed handling of a malformed config.

import json
import pathlib
import shutil
import tempfile
import unittest

from groklib import GrokWrapperError
from groklib import projectconfig


class ProjectConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-projectconfig-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.repo = pathlib.Path(self.tmp_root) / "repo"
        self.repo.mkdir()

    def _write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    def test_detects_pnpm_from_lockfile(self) -> None:
        self._write("package.json", "{}")
        self._write("pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        config = projectconfig.load_project_config(self.repo)
        self.assertEqual(config.package_manager, "pnpm")

    def test_detects_yarn_bun_npm_from_lockfiles(self) -> None:
        cases = (
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("package-lock.json", "npm"),
        )
        for lockfile, expected in cases:
            with self.subTest(lockfile=lockfile):
                repo = pathlib.Path(self.tmp_root) / expected
                repo.mkdir()
                (repo / "package.json").write_text("{}", encoding="utf-8")
                (repo / lockfile).write_text("x\n", encoding="utf-8")
                self.assertEqual(projectconfig.load_project_config(repo).package_manager, expected)

    def test_defaults_to_npm_with_manifest_no_lockfile(self) -> None:
        self._write("package.json", "{}")
        self.assertEqual(projectconfig.load_project_config(self.repo).package_manager, "npm")

    def test_no_manifest_no_lockfile_disables_gate(self) -> None:
        config = projectconfig.load_project_config(self.repo)
        self.assertIsNone(config.package_manager)
        self.assertEqual(config.never_build_workspaces, {})
        self.assertFalse(config.require_rule_file_parity)

    def test_config_overrides_are_applied(self) -> None:
        self._write("package.json", "{}")
        self._write("pnpm-lock.yaml", "x\n")
        self._write(
            ".grok-skills.json",
            json.dumps(
                {
                    "packageManager": "npm",
                    "ruleFileParity": True,
                    "neverBuildWorkspaces": {"@my/schemas": ["typecheck"]},
                }
            ),
        )
        config = projectconfig.load_project_config(self.repo)
        self.assertEqual(config.package_manager, "npm")
        self.assertTrue(config.require_rule_file_parity)
        self.assertEqual(config.never_build_workspaces, {"@my/schemas": ("typecheck",)})

    def test_config_package_manager_null_disables_gate(self) -> None:
        self._write("package.json", "{}")
        self._write("pnpm-lock.yaml", "x\n")
        self._write(".grok-skills.json", json.dumps({"packageManager": None}))
        self.assertIsNone(projectconfig.load_project_config(self.repo).package_manager)

    def test_malformed_json_fails_closed(self) -> None:
        self._write(".grok-skills.json", "{ not valid json ")
        with self.assertRaises(GrokWrapperError) as caught:
            projectconfig.load_project_config(self.repo)
        self.assertEqual(caught.exception.error_class, "validation-failure")

    def test_unknown_package_manager_fails_closed(self) -> None:
        self._write(".grok-skills.json", json.dumps({"packageManager": "cargo"}))
        with self.assertRaises(GrokWrapperError) as caught:
            projectconfig.load_project_config(self.repo)
        self.assertEqual(caught.exception.error_class, "validation-failure")

    def test_bad_never_build_shape_fails_closed(self) -> None:
        self._write(".grok-skills.json", json.dumps({"neverBuildWorkspaces": {"@x": "typecheck"}}))
        with self.assertRaises(GrokWrapperError) as caught:
            projectconfig.load_project_config(self.repo)
        self.assertEqual(caught.exception.error_class, "validation-failure")

    def test_build_gate_command_shapes(self) -> None:
        self.assertEqual(projectconfig.build_gate_command("pnpm", "build"), ["pnpm", "run", "build"])
        self.assertEqual(projectconfig.build_gate_command("npm", "test"), ["npm", "run", "test"])
        self.assertEqual(projectconfig.build_gate_command("bun", "lint"), ["bun", "run", "lint"])
        self.assertEqual(projectconfig.build_gate_command("yarn", "typecheck"), ["yarn", "typecheck"])

    def test_install_command_shapes(self) -> None:
        self.assertEqual(
            projectconfig.install_command("pnpm"), ["pnpm", "install", "--offline", "--frozen-lockfile"]
        )
        self.assertEqual(projectconfig.install_command("yarn"), ["yarn", "install", "--offline", "--frozen-lockfile"])
        self.assertEqual(projectconfig.install_command("bun"), ["bun", "install", "--frozen-lockfile"])
        self.assertEqual(
            projectconfig.install_command("npm"), ["npm", "install", "--offline", "--no-audit", "--no-fund"]
        )


if __name__ == "__main__":
    unittest.main()
