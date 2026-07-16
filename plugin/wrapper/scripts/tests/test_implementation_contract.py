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
