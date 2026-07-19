# wrapper/scripts/tests/test_deny_write_vectors.py
#
# Shared golden-vector parity guard for groklib.deny_write / path_matches_deny.
# Loads plugin/references/deny-write-globs.json (same SSOT Node tests use).

from __future__ import annotations

import pathlib
import unittest

from groklib.deny_write import (
    DENY_WRITE_GLOBS,
    deny_write_globs,
    deny_write_ssot_path,
    load_match_vectors,
    path_matches_deny,
)
from groklib.modes.direct_finalize import (
    DENY_WRITE_GLOBS as FINALIZE_GLOBS,
    path_matches_deny as finalize_path_matches_deny,
)


class DenyWriteSsotTests(unittest.TestCase):
    def test_ssot_file_present_and_schema(self) -> None:
        p = deny_write_ssot_path()
        self.assertTrue(p.is_file(), "missing deny-write SSOT at {}".format(p))
        self.assertEqual(p.name, "deny-write-globs.json")
        self.assertIn("references", p.parts)
        globs = deny_write_globs()
        self.assertGreaterEqual(len(globs), 10)
        self.assertIn(".env", globs)
        self.assertIn(".env.*", globs)
        self.assertIn("*.pem", globs)
        self.assertIn("credentials.json", globs)

    def test_compat_exports_identical_to_ssot(self) -> None:
        self.assertEqual(DENY_WRITE_GLOBS, deny_write_globs())
        self.assertEqual(FINALIZE_GLOBS, DENY_WRITE_GLOBS)
        self.assertIs(FINALIZE_GLOBS, DENY_WRITE_GLOBS)

    def test_match_vectors_python(self) -> None:
        vectors = load_match_vectors()
        cases = [
            c
            for c in vectors
            if not c.get("appliesTo") or "python" in c["appliesTo"]
        ]
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            with self.subTest(case["id"]):
                got = path_matches_deny(case["path"])
                self.assertEqual(
                    got,
                    case["expected"],
                    "vector {}: path={!r} got={} expected={}".format(
                        case["id"], case["path"], got, case["expected"]
                    ),
                )
                # Compat re-export must stay identical.
                self.assertEqual(
                    finalize_path_matches_deny(case["path"]),
                    case["expected"],
                    "finalize re-export diverged on {}".format(case["id"]),
                )

    def test_no_hardcoded_second_list_in_finalize_source(self) -> None:
        """direct_finalize must not redefine a private DENY_WRITE_GLOBS tuple."""
        src = pathlib.Path(__file__).resolve().parents[1] / "groklib" / "modes" / "direct_finalize.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("from groklib.deny_write import", text)
        # No local tuple assignment of DENY_WRITE_GLOBS = (
        self.assertNotRegex(
            text,
            r"^DENY_WRITE_GLOBS\s*=\s*\(",
            msg="direct_finalize must not host a second deny list",
        )


if __name__ == "__main__":
    unittest.main()
