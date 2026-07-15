# wrapper/scripts/tests/test_rules.py

import hashlib
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from groklib import rules
from groklib.rules import InstructionFile, RulesParityError

_SHARED_ROOT_HEADER = "<!-- AGENTS.md | CLAUDE.md -->\n"
_SHARED_FOO_HEADER = "<!-- apps/foo/AGENTS.md | CLAUDE.md -->\n"


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class RulesTests(unittest.TestCase):
    """Covers C7 instruction discovery, byte parity, header validation, and payload rendering."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="grok-cli-rules-test-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.repo_root = pathlib.Path(self.tmp_root) / "repo"
        self.repo_root.mkdir()

    def _build_two_level_tree(self) -> pathlib.Path:
        """Root pair + apps/foo pair, both shared-header, byte-identical bodies. Returns target dir."""
        _write(self.repo_root / "AGENTS.md", _SHARED_ROOT_HEADER + "Root rules.\n")
        _write(self.repo_root / "CLAUDE.md", _SHARED_ROOT_HEADER + "Root rules.\n")
        target = self.repo_root / "apps" / "foo"
        _write(target / "AGENTS.md", _SHARED_FOO_HEADER + "Foo rules.\n")
        _write(target / "CLAUDE.md", _SHARED_FOO_HEADER + "Foo rules.\n")
        return target

    def test_discovers_pairs_root_first_order(self) -> None:
        target = self._build_two_level_tree()

        discovered = rules.discover_instruction_files(self.repo_root, target)

        self.assertEqual(len(discovered), 2)
        self.assertEqual(discovered[0].repo_relative, "AGENTS.md")
        self.assertEqual(discovered[1].repo_relative, "apps/foo/AGENTS.md")
        self.assertEqual(discovered[0].path, (self.repo_root / "AGENTS.md").resolve())
        self.assertEqual(discovered[1].path, (target / "AGENTS.md").resolve())

    def test_level_without_any_instruction_files_is_skipped(self) -> None:
        # "apps" itself (between repo root and apps/foo) has no AGENTS.md/CLAUDE.md
        # pair of its own; only repo root and apps/foo do.
        target = self._build_two_level_tree()

        discovered = rules.discover_instruction_files(self.repo_root, target)

        levels = [instruction.repo_relative for instruction in discovered]
        self.assertNotIn("apps/AGENTS.md", levels)
        self.assertEqual(levels, ["AGENTS.md", "apps/foo/AGENTS.md"])

    def test_single_file_of_pair_raises_in_strict_mode(self) -> None:
        _write(self.repo_root / "AGENTS.md", _SHARED_ROOT_HEADER + "Root rules.\n")
        # No CLAUDE.md at repo root.

        with self.assertRaises(RulesParityError):
            rules.discover_instruction_files(self.repo_root, self.repo_root, require_parity=True)

    def test_single_file_of_pair_raises_in_strict_mode_reverse(self) -> None:
        _write(self.repo_root / "CLAUDE.md", _SHARED_ROOT_HEADER + "Root rules.\n")
        # No AGENTS.md at repo root.

        with self.assertRaises(RulesParityError):
            rules.discover_instruction_files(self.repo_root, self.repo_root, require_parity=True)

    def test_single_file_loads_alone_in_permissive_default(self) -> None:
        # Standalone repo-agnostic default: a repo carrying only ONE of the pair
        # (e.g. only CLAUDE.md) just works -- the single file is loaded, no error.
        _write(self.repo_root / "CLAUDE.md", "# House rules\n\nBe careful.\n")

        discovered = rules.discover_instruction_files(self.repo_root, self.repo_root)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].repo_relative, "CLAUDE.md")
        self.assertEqual(discovered[0].content_bytes, b"# House rules\n\nBe careful.\n")

    def test_agents_only_loads_alone_in_permissive_default(self) -> None:
        _write(self.repo_root / "AGENTS.md", "# House rules\n\nBe careful.\n")

        discovered = rules.discover_instruction_files(self.repo_root, self.repo_root)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].repo_relative, "AGENTS.md")

    def test_permissive_ignores_divergent_bodies_and_headers(self) -> None:
        # Permissive default: a repo with both files, divergent bodies and no
        # path-header convention, is NOT rejected -- AGENTS.md is loaded verbatim.
        _write(self.repo_root / "AGENTS.md", "# Agents\n\nAgent rules.\n")
        _write(self.repo_root / "CLAUDE.md", "# Claude\n\nDifferent rules.\n")

        discovered = rules.discover_instruction_files(self.repo_root, self.repo_root)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].repo_relative, "AGENTS.md")
        self.assertEqual(discovered[0].content_bytes, b"# Agents\n\nAgent rules.\n")

    def test_byte_parity_mismatch_raises_in_strict_mode(self) -> None:
        _write(self.repo_root / "AGENTS.md", _SHARED_ROOT_HEADER + "Root rules.\n")
        _write(self.repo_root / "CLAUDE.md", _SHARED_ROOT_HEADER + "Root rules!\n")

        with self.assertRaises(RulesParityError):
            rules.discover_instruction_files(self.repo_root, self.repo_root, require_parity=True)

    def test_shared_header_accepted_in_strict_mode(self) -> None:
        _write(self.repo_root / "AGENTS.md", _SHARED_ROOT_HEADER + "Root rules.\n")
        _write(self.repo_root / "CLAUDE.md", _SHARED_ROOT_HEADER + "Root rules.\n")

        discovered = rules.discover_instruction_files(self.repo_root, self.repo_root, require_parity=True)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].repo_relative, "AGENTS.md")

    def test_legacy_per_file_headers_accepted_in_strict_mode(self) -> None:
        # Not-yet-migrated pair: each file's line 1 names itself; bodies
        # (bytes after line 1) are byte-identical.
        _write(self.repo_root / "AGENTS.md", "<!-- AGENTS.md -->\nRoot rules.\n")
        _write(self.repo_root / "CLAUDE.md", "<!-- CLAUDE.md -->\nRoot rules.\n")

        discovered = rules.discover_instruction_files(self.repo_root, self.repo_root, require_parity=True)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].repo_relative, "AGENTS.md")
        self.assertEqual(discovered[0].content_bytes, b"<!-- AGENTS.md -->\nRoot rules.\n")

    def test_wrong_header_path_raises_in_strict_mode(self) -> None:
        _write(self.repo_root / "AGENTS.md", "<!-- totally/wrong/path.md -->\nRoot rules.\n")
        _write(self.repo_root / "CLAUDE.md", "<!-- totally/wrong/path.md -->\nRoot rules.\n")

        with self.assertRaises(RulesParityError):
            rules.discover_instruction_files(self.repo_root, self.repo_root, require_parity=True)

    def test_case_variant_duplicate_raises(self) -> None:
        # The real macOS filesystem is case-insensitive, so two case-variant
        # names cannot coexist on disk; simulate the ambiguous listing a
        # case-sensitive filesystem (or a case mismatch bug) could produce
        # by patching os.listdir's return value directly, and confirm the
        # discovery code path flags it rather than silently picking one.
        with mock.patch(
            "groklib.rules.os.listdir",
            return_value=["AGENTS.md", "Agents.md", "CLAUDE.md"],
        ):
            with self.assertRaises(RulesParityError):
                rules.discover_instruction_files(self.repo_root, self.repo_root)

    def test_invalid_utf8_body_raises(self) -> None:
        agents_path = self.repo_root / "AGENTS.md"
        claude_path = self.repo_root / "CLAUDE.md"
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        invalid_content = _SHARED_ROOT_HEADER.encode("utf-8") + b"\xff\xfe not valid utf-8\n"
        agents_path.write_bytes(invalid_content)
        claude_path.write_bytes(invalid_content)

        with self.assertRaises(RulesParityError):
            rules.discover_instruction_files(self.repo_root, self.repo_root)

    def test_target_outside_repo_root_raises(self) -> None:
        other_root = pathlib.Path(self.tmp_root) / "other"
        other_root.mkdir()

        with self.assertRaises(RulesParityError):
            rules.discover_instruction_files(self.repo_root, other_root)

    def test_sha256_and_byte_counts_recorded(self) -> None:
        content = _SHARED_ROOT_HEADER + "Root rules.\n"
        _write(self.repo_root / "AGENTS.md", content)
        _write(self.repo_root / "CLAUDE.md", content)
        expected_bytes = content.encode("utf-8")
        expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()

        discovered = rules.discover_instruction_files(self.repo_root, self.repo_root)

        self.assertEqual(len(discovered), 1)
        instruction = discovered[0]
        self.assertEqual(instruction.content_bytes, expected_bytes)
        self.assertEqual(instruction.sha256, expected_sha256)
        self.assertIsInstance(instruction, InstructionFile)

        entries = rules.instruction_envelope_entries(discovered)
        self.assertEqual(
            entries,
            [{"path": "AGENTS.md", "bytes": len(expected_bytes), "sha256": expected_sha256}],
        )

    def test_payload_matches_c7_template_exactly(self) -> None:
        target = self._build_two_level_tree()
        discovered = rules.discover_instruction_files(self.repo_root, target)

        payload = rules.build_prompt_payload(discovered, "Do the thing.")

        expected = (
            "=== REPOSITORY RULES (governing; read completely before the task) ===\n"
            "--- BEGIN AGENTS.md ---\n"
            "<!-- AGENTS.md | CLAUDE.md -->\n"
            "Root rules.\n"
            "--- END AGENTS.md ---\n"
            "--- BEGIN apps/foo/AGENTS.md ---\n"
            "<!-- apps/foo/AGENTS.md | CLAUDE.md -->\n"
            "Foo rules.\n"
            "--- END apps/foo/AGENTS.md ---\n"
            "=== TASK ===\n"
            "Do the thing."
        )
        self.assertEqual(payload, expected)

    def test_build_prompt_payload_with_no_instructions(self) -> None:
        payload = rules.build_prompt_payload([], "Do the thing.")

        expected = (
            "=== REPOSITORY RULES (governing; read completely before the task) ===\n"
            "=== TASK ===\n"
            "Do the thing."
        )
        self.assertEqual(payload, expected)

    def test_instruction_envelope_entries_shape(self) -> None:
        target = self._build_two_level_tree()
        discovered = rules.discover_instruction_files(self.repo_root, target)

        entries = rules.instruction_envelope_entries(discovered)

        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(set(entry.keys()), {"path", "bytes", "sha256"})
            self.assertIsInstance(entry["path"], str)
            self.assertIsInstance(entry["bytes"], int)
            self.assertIsInstance(entry["sha256"], str)


if __name__ == "__main__":
    unittest.main()
