# wrapper/scripts/tests/test_source_secret_policy.py
#
# Fail-closed source scan: committed test fixtures must not hold contiguous
# secret-shaped literals (AGENTS.md #8). Patterns reuse the production value
# table labels that govern GitHub/Slack/xAI/OpenAI/Anthropic/AWS/JWT shapes;
# scanning source text (not redaction behavior) keeps this non-tautological.

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List, Tuple

from groklib.redaction import _SECRET_VALUE_PATTERNS

# Labels already governed by repository secret-value patterns that fixtures
# commonly reconstruct. Bearer prose and PEM blocks are out of this policy's
# prefix-focused scope (still split elsewhere when practical).
_FIXTURE_POLICY_LABELS = frozenset(
    {
        "api-key-token",
        "jwt",
        "aws-access-key-id",
        "github-token",
        "slack-token",
    }
)

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parents[2]
_OWNED_SOURCES: Tuple[Path, ...] = (
    _TESTS_DIR / "test_redaction.py",
    _PLUGIN_DIR / "scripts" / "tests" / "progress-relay.test.mjs",
    Path(__file__).resolve(),
)


def _fixture_policy_patterns():
    return tuple(
        pattern
        for label, pattern in _SECRET_VALUE_PATTERNS
        if label in _FIXTURE_POLICY_LABELS
    )


def scan_source_for_contiguous_secret_shapes(text: str) -> List[Tuple[str, int, str]]:
    """Return (label, line_no, match) for every contiguous fixture-policy hit."""
    hits: List[Tuple[str, int, str]] = []
    for label, pattern in _SECRET_VALUE_PATTERNS:
        if label not in _FIXTURE_POLICY_LABELS:
            continue
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            hits.append((label, line_no, match.group(0)))
    return hits


class SourceSecretFixturePolicyTests(unittest.TestCase):
    """AGENTS.md #8: no contiguous secret-shaped literals in owned test sources."""

    def test_owned_sources_have_no_contiguous_secret_shaped_literals(self) -> None:
        self.assertTrue(
            _fixture_policy_patterns(),
            "fixture policy must bind at least one production pattern",
        )
        failures: List[str] = []
        for path in _OWNED_SOURCES:
            self.assertTrue(path.is_file(), "owned source missing: {}".format(path))
            text = path.read_text(encoding="utf-8")
            for label, line_no, matched in scan_source_for_contiguous_secret_shapes(text):
                failures.append(
                    "{}:{}: [{}] contiguous secret-shaped literal {!r}".format(
                        path.name, line_no, label, matched[:48]
                    )
                )
        if failures:
            self.fail(
                "contiguous secret-shaped fixture literals (split strings required):\n"
                + "\n".join(failures)
            )


if __name__ == "__main__":
    unittest.main()
