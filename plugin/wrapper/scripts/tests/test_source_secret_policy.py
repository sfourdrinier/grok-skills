# wrapper/scripts/tests/test_source_secret_policy.py
#
# Fail-closed source scan: committed test fixtures must not hold contiguous
# secret-shaped literals (AGENTS.md #8). Patterns reuse the full production
# value table that governs bearer/JWT/AWS/GitHub/Slack/xAI/OpenAI/Anthropic/
# webhook/PEM shapes; scanning source text (not redaction behavior) keeps this
# non-tautological.
#
# Regex / pattern definition sites are excluded with precise path+line spans so
# production pattern sources remain scannable for accidental fixture bleed while
# the definitions themselves do not self-fail.

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

from groklib.redaction import _SECRET_VALUE_PATTERNS

_THIS_FILE = Path(__file__).resolve()
_WRAPPER_SCRIPTS = _THIS_FILE.parents[1]
_PLUGIN_DIR = _THIS_FILE.parents[3]
_TESTS_ROOTS: Tuple[Path, ...] = (
    _THIS_FILE.parent,
    _PLUGIN_DIR / "scripts" / "tests",
)

_SOURCE_SUFFIXES = frozenset({".py", ".mjs", ".js", ".json", ".md", ".txt", ".sh"})

# Precise path + 1-indexed line spans that hold production pattern definitions
# (not fixtures). Only these lines are skipped; every other line is fail-closed.
_PATTERN_DEFINITION_SPANS: Tuple[Tuple[Path, int, int], ...] = (
    (
        _WRAPPER_SCRIPTS / "groklib" / "redaction.py",
        175,
        184,
    ),
    (
        _PLUGIN_DIR / "scripts" / "progress-relay.mjs",
        87,
        99,
    ),
)


def _is_scannable_source(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix not in _SOURCE_SUFFIXES:
        return False
    # Only inspect path components under the fixture root so a parent directory
    # like ".claude/worktrees/..." never blanks the whole inventory.
    try:
        relative_parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return False
    if any(part == "__pycache__" or part.startswith(".") for part in relative_parts):
        return False
    return True


def iter_fixture_source_paths(roots: Sequence[Path] = _TESTS_ROOTS) -> List[Path]:
    """Collect every committed test / helper source under the fixture roots."""
    found: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if _is_scannable_source(path, root):
                found.append(path)
    return found


def _excluded_line_numbers(path: Path) -> Set[int]:
    excluded: Set[int] = set()
    resolved = path.resolve()
    for span_path, start, end in _PATTERN_DEFINITION_SPANS:
        if resolved == span_path.resolve():
            excluded.update(range(start, end + 1))
    return excluded


def scan_source_for_contiguous_secret_shapes(
    text: str,
    *,
    excluded_lines: Iterable[int] = (),
) -> List[Tuple[str, int, str]]:
    """Return (label, line_no, match) for every contiguous production-pattern hit.

    ``excluded_lines`` are 1-indexed line numbers skipped with path+line precision
    (pattern-definition sites only). Fixture content is never exempted by label
    or whole-file ignore.
    """
    skip = frozenset(excluded_lines)
    hits: List[Tuple[str, int, str]] = []
    for label, pattern in _SECRET_VALUE_PATTERNS:
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            if line_no in skip:
                continue
            hits.append((label, line_no, match.group(0)))
    return hits


class SourceSecretFixturePolicyTests(unittest.TestCase):
    """AGENTS.md #8: no contiguous secret-shaped literals in test sources."""

    def test_production_patterns_are_all_bound(self) -> None:
        labels = {label for label, _ in _SECRET_VALUE_PATTERNS}
        self.assertGreaterEqual(len(labels), 8)
        for required in (
            "bearer-token",
            "api-key-token",
            "jwt",
            "aws-access-key-id",
            "github-token",
            "slack-token",
            "slack-webhook",
            "pem-private-key",
        ):
            self.assertIn(required, labels)

    def test_fixture_sources_have_no_contiguous_secret_shaped_literals(self) -> None:
        sources = iter_fixture_source_paths()
        self.assertTrue(sources, "fixture source inventory must not be empty")
        # Helpers living beside tests (e.g. fake_grok.py) are included via rglob.
        self.assertTrue(
            any(path.name == "fake_grok.py" for path in sources),
            "fake_grok.py must be in the scanned inventory",
        )
        failures: List[str] = []
        for path in sources:
            text = path.read_text(encoding="utf-8")
            excluded = _excluded_line_numbers(path)
            for label, line_no, matched in scan_source_for_contiguous_secret_shapes(
                text, excluded_lines=excluded
            ):
                preview = matched.replace("\n", "\\n")[:48]
                failures.append(
                    "{}:{}: [{}] contiguous secret-shaped literal {!r}".format(
                        path, line_no, label, preview
                    )
                )
        if failures:
            self.fail(
                "contiguous secret-shaped fixture literals (split strings required):\n"
                + "\n".join(failures)
            )

    def test_pattern_definition_spans_are_precise_not_file_wide(self) -> None:
        # The exclusion mechanism only drops definition lines; a fixture leak on
        # another line of the same file must still fail closed.
        redaction = _WRAPPER_SCRIPTS / "groklib" / "redaction.py"
        self.assertTrue(redaction.is_file())
        excluded = _excluded_line_numbers(redaction)
        self.assertTrue(excluded)
        # A synthetic leak outside the definition span is not excluded.
        # Build the leak at runtime so THIS source file stays policy-clean.
        leak_line = max(excluded) + 5
        self.assertNotIn(leak_line, excluded)
        leak = "Bearer " + "abc123" + "def456" + "ABCDEF"
        synthetic = "safe\n" * (leak_line - 1) + leak + "\n"
        hits = scan_source_for_contiguous_secret_shapes(
            synthetic, excluded_lines=excluded
        )
        self.assertTrue(any(label == "bearer-token" for label, _, _ in hits))


if __name__ == "__main__":
    unittest.main()
