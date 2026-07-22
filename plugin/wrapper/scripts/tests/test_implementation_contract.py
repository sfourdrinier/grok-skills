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
    normalize_git_repo_path,
    normalize_repo_relative,
    path_in_scopes,
    trust_model,
    validate_contract,
)


class MultiViolationContractTests(unittest.TestCase):
    def test_reports_all_violations_in_one_error(self) -> None:
        # Issue #8: one round-trip, not one error per launch.
        with self.assertRaises(GrokWrapperError) as cm:
            validate_contract(
                {
                    "schemaVersion": 2,
                    "taskId": "!!!bad!!!",
                    "target": "",
                    "writeScopes": ["pkg/a.ts"],
                }
            )
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        violations = (cm.exception.detail or {}).get("violations") or []
        self.assertGreaterEqual(len(violations), 3, violations)
        joined = " ".join(violations).lower()
        self.assertIn("schemaversion", joined)
        self.assertIn("taskid", joined)
        self.assertIn("target", joined)
        self.assertIn("writescopes", joined)



class OnlyIfChangedTests(unittest.TestCase):
    def test_validation_matches_changed(self) -> None:
        from groklib.implementation_contract import validation_matches_changed

        always = {"argv": ["true"], "cwd": "."}
        self.assertTrue(validation_matches_changed(always, []))
        scoped = {
            "argv": ["true"],
            "cwd": ".",
            "onlyIfChanged": ["packages/foo"],
        }
        self.assertFalse(validation_matches_changed(scoped, ["packages/bar/a.ts"]))
        self.assertTrue(validation_matches_changed(scoped, ["packages/foo/x.ts"]))
        self.assertTrue(validation_matches_changed(scoped, ["packages/foo"]))
        root = {"argv": ["true"], "cwd": ".", "onlyIfChanged": ["."]}
        self.assertTrue(validation_matches_changed(root, ["any/path.ts"]))

    def test_only_if_changed_parsed_on_valid_contract(self) -> None:
        c = validate_contract(
            {
                "schemaVersion": 1,
                "taskId": "task-1",
                "target": ".",
                "writeScopes": [{"kind": "subtree", "path": "packages/foo"}],
                "requiredValidation": [
                    {
                        "argv": ["true"],
                        "cwd": ".",
                        "onlyIfChanged": ["packages/foo"],
                    }
                ],
            }
        )
        self.assertEqual(c["requiredValidation"][0]["onlyIfChanged"], ["packages/foo"])

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

    def test_operator_rejects_windows_drive_git_preserves_colon_filename(self) -> None:
        # Operator contract paths: drive-letter forms are absolute Windows paths.
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("C:/Users/me/repo/file.ts")
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("D:secret")
        # Bypass via ./ prefix must still fail closed after component clean.
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("./C:/Users/me/x")
        with self.assertRaises(GrokWrapperError):
            normalize_repo_relative("./C:Users/me")
        # Git-reported paths: second-char colon is a legal filename component.
        self.assertEqual(normalize_git_repo_path("a:b.txt"), "a:b.txt")
        self.assertEqual(normalize_git_repo_path("pkg/a:b.txt"), "pkg/a:b.txt")
        # Absolute Git paths still rejected.
        with self.assertRaises(GrokWrapperError):
            normalize_git_repo_path("/etc/passwd")
        with self.assertRaises(GrokWrapperError):
            normalize_git_repo_path("../escape")


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

    def test_git_path_colon_filename_in_scope(self) -> None:
        # Operator writeScopes cannot name ``a:b.txt`` as a file path (drive-letter
        # rejection); root/subtree scopes still cover Git-reported colon names.
        scopes = [{"kind": "subtree", "path": "."}]
        self.assertTrue(path_in_scopes("a:b.txt", scopes, from_git=True))
        scopes_pkg = [{"kind": "subtree", "path": "pkg"}]
        self.assertTrue(path_in_scopes("pkg/a:b.txt", scopes_pkg, from_git=True))
        self.assertFalse(path_in_scopes("a:b.txt", scopes_pkg, from_git=True))


class LoadContractFileTests(unittest.TestCase):
    def test_missing_and_directory_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaises(GrokWrapperError):
                load_contract_file(root / "nope.json")
            d = root / "dir"
            d.mkdir()
            with self.assertRaises(GrokWrapperError):
                load_contract_file(d)


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

    def test_argv_nul_bytes_rejected(self) -> None:
        with self.assertRaises(GrokWrapperError) as cm:
            self._ok(requiredValidation=[{"argv": ["true", "bad\x00arg"], "cwd": "."}])
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        self.assertIn("NUL", str(cm.exception))

    def test_trust_model_constant(self) -> None:
        self.assertEqual(trust_model(), "operator-contract-trusted-no-os-sandbox")
        c = self._ok()
        self.assertEqual(c["trustModel"], "operator-contract-trusted-no-os-sandbox")

    def test_target_match(self) -> None:
        c = self._ok(target="pkg")
        assert_target_matches(c, "pkg")
        with self.assertRaises(GrokWrapperError):
            assert_target_matches(c, "other")

    def test_objective_over_cap_rejected(self) -> None:
        # Phase 1 finding 4: objective must be <= 2000 chars (fail closed at load).
        with self.assertRaises(GrokWrapperError) as cm:
            self._ok(objective="x" * 2001)
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        self.assertIn("objective", str(cm.exception).lower())
        # Boundary: exactly 2000 is accepted.
        c = self._ok(objective="y" * 2000)
        self.assertEqual(len(c["objective"]), 2000)

    def test_acceptance_criteria_over_cap_rejected(self) -> None:
        # Phase 1 finding 4: <= 32 items, each string <= 500 chars after strip.
        with self.assertRaises(GrokWrapperError) as cm:
            self._ok(acceptanceCriteria=["c{}".format(i) for i in range(33)])
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        self.assertIn("acceptanceCriteria", str(cm.exception))

        with self.assertRaises(GrokWrapperError) as cm:
            self._ok(acceptanceCriteria=["z" * 501])
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
        self.assertIn("acceptanceCriteria", str(cm.exception))

        # Strip before measuring: leading/trailing space must not hide over-cap body.
        with self.assertRaises(GrokWrapperError) as cm:
            self._ok(acceptanceCriteria=["  " + ("w" * 501) + "  "])
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")

        # Non-string criteria rejected (fail closed).
        with self.assertRaises(GrokWrapperError) as cm:
            self._ok(acceptanceCriteria=[123])
        self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")

        # Boundary: 32 items of 500 chars after strip are accepted.
        c = self._ok(acceptanceCriteria=["  " + ("a" * 500) + "  "] * 32)
        self.assertEqual(len(c["acceptanceCriteria"]), 32)


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

    def test_rejects_invalid_utf8_contract_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bad.json"
            path.write_bytes(b"\xff\xfe{not utf8")
            with self.assertRaises(GrokWrapperError) as cm:
                load_contract_file(path)
            self.assertEqual(cm.exception.error_class, "implementation-contract-invalid")
            self.assertIn("UTF-8", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
