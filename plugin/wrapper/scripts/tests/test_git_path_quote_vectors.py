# wrapper/scripts/tests/test_git_path_quote_vectors.py
#
# Shared golden-vector parity guard for groklib.git_path_quote. Loads the same
# plugin/references/git-c-quoted-path-vectors.json Node tests use. No runtime
# cross-language dependency - pure data fixture.

from __future__ import annotations

import json
import pathlib
import unittest

from groklib.git_path_quote import (
    decode_git_c_quoted_token,
    parse_diff_git_header_paths,
)
from groklib.implementation_handoff import paths_from_git_patch

# plugin/references/git-c-quoted-path-vectors.json relative to this test file:
# tests/ -> scripts/ -> wrapper/ -> plugin/ -> references/
_VECTORS_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "references"
    / "git-c-quoted-path-vectors.json"
)


def _load_vectors() -> dict:
    with _VECTORS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class GitPathQuoteGoldenVectorTests(unittest.TestCase):
    def test_vectors_file_is_present(self) -> None:
        self.assertTrue(
            _VECTORS_PATH.is_file(),
            "missing shared golden vectors at {}".format(_VECTORS_PATH),
        )
        doc = _load_vectors()
        self.assertEqual(doc.get("schemaVersion"), 1)
        self.assertTrue(doc.get("tokenDecode"))

    def test_token_decode_matches_shared_vectors(self) -> None:
        doc = _load_vectors()
        cases = [
            c
            for c in doc["tokenDecode"]
            if not c.get("appliesTo") or "python" in c["appliesTo"]
        ]
        self.assertGreaterEqual(len(cases), 8)
        for case in cases:
            with self.subTest(case["id"]):
                got = decode_git_c_quoted_token(case["input"])
                self.assertEqual(
                    got,
                    case["expected"],
                    "vector {}: input={!r} got={!r} expected={!r}".format(
                        case["id"], case["input"], got, case["expected"]
                    ),
                )

    def test_diff_git_header_vectors(self) -> None:
        doc = _load_vectors()
        for case in doc.get("diffGitHeaders") or []:
            if case.get("appliesTo") and "python" not in case["appliesTo"]:
                continue
            with self.subTest(case["id"]):
                got = parse_diff_git_header_paths(case["rest"])
                expected = case["expected"]
                if expected is None:
                    self.assertIsNone(got)
                else:
                    self.assertEqual(list(got), list(expected))

    def test_dev_null_exclusion_vectors(self) -> None:
        doc = _load_vectors()
        for case in doc.get("devNullExclusion") or []:
            if case.get("appliesTo") and "python" not in case["appliesTo"]:
                continue
            with self.subTest(case["id"]):
                if "patchText" in case:
                    paths = paths_from_git_patch(case["patchText"].encode("utf-8"))
                    self.assertEqual(paths, set(case["expectedPaths"]))
                elif "rest" in case:
                    raw = parse_diff_git_header_paths(case["rest"])
                    self.assertIsNotNone(raw)
                    self.assertEqual(list(raw), list(case["expectedRaw"]))
                    # Dedup + exclude /dev/null the same way paths_from_git_patch does.
                    filtered = []
                    seen = set()
                    for p in raw:
                        if p and p != "/dev/null" and p not in seen:
                            seen.add(p)
                            filtered.append(p)
                    self.assertEqual(filtered, list(case["expectedFiltered"]))

    def test_malformed_fail_closed_vectors(self) -> None:
        doc = _load_vectors()
        for case in doc.get("malformedFailClosed") or []:
            if case.get("appliesTo") and "python" not in case["appliesTo"]:
                continue
            with self.subTest(case["id"]):
                got = parse_diff_git_header_paths(case["rest"])
                self.assertIsNone(got, "malformed header must fail closed (None)")


if __name__ == "__main__":
    unittest.main()
