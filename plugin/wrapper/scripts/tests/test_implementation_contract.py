# wrapper/scripts/tests/test_implementation_contract.py

import json
import os
import pathlib
import tempfile
import unittest

from groklib import GrokWrapperError
from groklib.implementation_contract import (
    assert_target_matches,
    load_contract_file,
    normalize_repo_relative,
    path_in_scopes,
    trust_model,
    validate_contract,
)


class NormalizePathTests(unittest.TestCase):
    def test_rejects_absolute_and_traversal(self) -> None:
        with self.assertRaises(GrokWrapperError) as cm:
            normalize_repo_relative("/etc/passwd")
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("../secret")
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("a/../../b")
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("")

    def test_normalizes_slashes(self) -> None:
        self.assertEqual(normalize_repo_relative("pkg//src/a.ts"), "pkg/src/a.ts")


class PathInScopesTests(unittest.TestCase):
    def test_file_exact_not_prefix(self) -> None:
        scopes = [{"kind": "file", "path": "pkg/a.ts"}]
        self.assertTrue(path_in_scopes("pkg/a.ts", scopes))
        self.assertFalse(path_in_scopes("pkg/a.ts.bak", scopes))
        self.assertFalse(path_in_scopes("pkg/a.ts/extra", scopes))

    def test_subtree_component_prefix_not_string_prefix(self) -> None:
        scopes = [{"kind": "subtree", "path": "pkg/src"}]
        self.assertTrue(path_in_scopes("pkg/src/x.ts", scopes))
        self.assertTrue(path_in_scopes("pkg/src", scopes))
        # string-prefix false friend: pkg/src2 is NOT under pkg/src
        self.assertFalse(path_in_scopes("pkg/src2/x.ts", scopes))
        self.assertFalse(path_in_scopes("pkg/other/x.ts", scopes))

    def test_subtree_dot_is_repo_root_wildcard(self) -> None:
        scopes = [{"kind": "subtree", "path": "."}]
        self.assertTrue(path_in_scopes("pkg/a.ts", scopes))
        self.assertTrue(path_in_scopes("a.ts", scopes))
        self.assertTrue(path_in_scopes("deep/nested/x", scopes))

    def test_git_path_preserves_backslash_literal(self) -> None:
        # POSIX: backslash is a filename character, not a separator for Git paths.
        scopes = [{"kind": "subtree", "path": "pkg"}]
        self.assertFalse(path_in_scopes(r"pkg\evil.ts", scopes, from_git=True))
        self.assertTrue(path_in_scopes("pkg/evil.ts", scopes, from_git=True))
        # Operator-supplied scopes still accept Windows-style separators.
        self.assertTrue(path_in_scopes(r"pkg\evil.ts", scopes, from_git=False))

    def test_git_path_preserves_trailing_whitespace(self) -> None:
        scopes = [{"kind": "file", "path": "pkg/allowed.ts"}]
        # Trailing space is a different filename; must not match file scope without it.
        self.assertFalse(path_in_scopes("pkg/allowed.ts ", scopes, from_git=True))
        self.assertTrue(path_in_scopes("pkg/allowed.ts", scopes, from_git=True))


class ValidateContractTests(unittest.TestCase):
    def _ok(self, **overrides):
        base = {
            "schemaVersion": 1,
            "taskId": "task-1",
            "target": "pkg",
            "writeScopes": [{"kind": "subtree", "path": "pkg"}],
            "requiredValidation": [],
        }
        base.update(overrides)
        return validate_contract(base)

    def test_empty_scopes_rejected(self) -> None:
        with self.assertRaises(GrokWrapperError):
            self._ok(writeScopes=[])

    def test_argv_must_be_array(self) -> None:
        with self.assertRaises(GrokWrapperError):
            self._ok(
                requiredValidation=[{"argv": "pnpm test", "cwd": "."}]
            )

    def test_falsey_required_validation_rejected(self) -> None:
        for bad in ("", False, 0, {}):
            with self.assertRaises(GrokWrapperError) as cm:
                self._ok(requiredValidation=bad)
            self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        # absent / null still means no validations
        c = self._ok()
        self.assertEqual(c["requiredValidation"], [])
        c2 = validate_contract(
            {
                "schemaVersion": 1,
                "taskId": "t1",
                "target": "pkg",
                "writeScopes": [{"kind": "subtree", "path": "pkg"}],
                "requiredValidation": None,
            }
        )
        self.assertEqual(c2["requiredValidation"], [])

    def test_trust_model_constant(self) -> None:
        self.assertEqual(trust_model(), "operator-contract-trusted-no-os-sandbox")
        c = self._ok()
        self.assertEqual(c["trustModel"], "operator-contract-trusted-no-os-sandbox")

    def test_target_match(self) -> None:
        c = self._ok(target="pkg")
        assert_target_matches(c, "pkg")
        with self.assertRaises(GrokWrapperError):
            assert_target_matches(c, "other")


class LoadContractFileTests(unittest.TestCase):
    def test_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real.json"
            real.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": "t1",
                        "target": ".",
                        "writeScopes": [{"kind": "file", "path": "a.ts"}],
                    }
                ),
                encoding="utf-8",
            )
            link = pathlib.Path(tmp) / "link.json"
            os.symlink(str(real), str(link))
            with self.assertRaises(GrokWrapperError) as cm:
                load_contract_file(link)
            self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")

    def test_rejects_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = pathlib.Path(tmp) / "realdir"
            real_dir.mkdir()
            real = real_dir / "c.json"
            real.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": "t1",
                        "target": ".",
                        "writeScopes": [{"kind": "file", "path": "a.ts"}],
                    }
                ),
                encoding="utf-8",
            )
            link_dir = pathlib.Path(tmp) / "contracts"
            os.symlink(str(real_dir), str(link_dir))
            with self.assertRaises(GrokWrapperError) as cm:
                load_contract_file(link_dir / "c.json")
            self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
            self.assertIn("symlink", str(cm.exception).lower())

    def test_loads_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "c.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": "t1",
                        "target": ".",
                        "writeScopes": [{"kind": "subtree", "path": "src"}],
                        "requiredValidation": [
                            {"argv": ["true"], "cwd": ".", "purpose": "noop"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            c = load_contract_file(path)
            self.assertEqual(c["taskId"], "t1")
            self.assertEqual(c["requiredValidation"][0]["argv"], ["true"])


if __name__ == "__main__":
    unittest.main()
