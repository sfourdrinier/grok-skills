# wrapper/scripts/groklib/rules.py
#
# C7 rules payload: discovers AGENTS.md/CLAUDE.md instruction files governing a
# target workspace (repo root down to the target, one level per path component)
# and renders the exact prompt template full-context modes hand to Grok before
# the task body. Default mode is permissive (load whichever files exist). When
# ProjectConfig.require_rule_file_parity is true, byte parity is enforced on the
# file BODY (everything after the first line) and an optional path-header
# convention may be validated for pairs that carry matching headers.

import dataclasses
import hashlib
import os
import pathlib
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr

_AGENTS_FILENAME = "AGENTS.md"
_CLAUDE_FILENAME = "CLAUDE.md"
_RULES_BANNER = "=== REPOSITORY RULES (governing; read completely before the task) ==="
_TASK_BANNER = "=== TASK ==="


def _log_stderr(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "rules" component prefix."""
    log_stderr("rules", function, message)


class RulesParityError(GrokWrapperError):
    """Raised for any C7 violation: missing pair member, byte-parity mismatch,
    invalid or mismatched path header, a case-variant duplicate at one
    level, or a target outside the repo root.
    """

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("rules-parity-failure", message, detail)


@dataclasses.dataclass(frozen=True)
class InstructionFile:
    path: pathlib.Path
    repo_relative: str
    content_bytes: bytes
    sha256: str


def _split_header(content: bytes) -> Tuple[bytes, bytes]:
    """Split ``content`` into (header line including its trailing newline, remaining body)."""
    newline_index = content.find(b"\n")
    if newline_index == -1:
        return content, b""
    return content[: newline_index + 1], content[newline_index + 1 :]


def _posix_dir_relative(level_dir: pathlib.Path, repo_root: pathlib.Path) -> str:
    """Return the repo-relative directory path for ``level_dir`` as POSIX text, "" at repo root."""
    if level_dir == repo_root:
        return ""
    return level_dir.relative_to(repo_root).as_posix()


def _join_repo_relative(dir_relative: str, filename: str) -> str:
    if dir_relative == "":
        return filename
    return dir_relative + "/" + filename


def _scan_level(level_dir: pathlib.Path) -> Tuple[Optional[str], Optional[str]]:
    """Case-insensitively discover AGENTS.md/CLAUDE.md at ``level_dir``, returning exact on-disk names.

    Returns (agents_name, claude_name); either or both are None when absent.
    A missing or unreadable directory (not yet created because the target
    path does not exist that deep) is treated as "no instruction files at
    this level", not an error: the walk continues to the next level. Two
    entries at one level that differ only by case for the same canonical
    filename (hazard 6: the case-insensitive macOS filesystem) is always a
    RulesParityError, since it means the discovery result is ambiguous.
    """
    try:
        entries = os.listdir(str(level_dir))
    except OSError as exc:
        _log_stderr("_scan_level", "no readable directory at {}: {}".format(level_dir, exc))
        return None, None

    agents_matches = sorted(entry for entry in entries if entry.lower() == _AGENTS_FILENAME.lower())
    claude_matches = sorted(entry for entry in entries if entry.lower() == _CLAUDE_FILENAME.lower())

    if len(agents_matches) > 1:
        raise RulesParityError(
            "multiple case-variant {} files at {}: {}".format(_AGENTS_FILENAME, level_dir, agents_matches),
            {"level": str(level_dir), "matches": agents_matches, "canonicalName": _AGENTS_FILENAME},
        )
    if len(claude_matches) > 1:
        raise RulesParityError(
            "multiple case-variant {} files at {}: {}".format(_CLAUDE_FILENAME, level_dir, claude_matches),
            {"level": str(level_dir), "matches": claude_matches, "canonicalName": _CLAUDE_FILENAME},
        )

    agents_name = agents_matches[0] if agents_matches else None
    claude_name = claude_matches[0] if claude_matches else None
    return agents_name, claude_name


def _read_instruction_bytes(path: pathlib.Path) -> bytes:
    """Read and return the complete raw bytes of one instruction file, verifying they are valid UTF-8.

    The C7 payload template embeds "complete file bytes, UTF-8" directly
    into the prompt text, so a file that is not valid UTF-8 must fail
    closed here, at discovery time, rather than reaching
    ``build_prompt_payload`` and raising an uncaught ``UnicodeDecodeError``
    later.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        _log_stderr("_read_instruction_bytes", "failed reading {}: {}".format(path, exc))
        raise RulesParityError(
            "failed to read instruction file {}: {}".format(path, exc),
            {"path": str(path)},
        ) from exc

    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        _log_stderr("_read_instruction_bytes", "instruction file is not valid UTF-8 at {}: {}".format(path, exc))
        raise RulesParityError(
            "instruction file is not valid UTF-8: {}".format(path),
            {"path": str(path)},
        ) from exc

    return content


def _validate_header(
    header_bytes: bytes,
    dir_relative: str,
    repo_relative_of_file: str,
    file_path: pathlib.Path,
) -> None:
    """Verify one file's line 1 matches the shared-header or legacy per-file header convention."""
    try:
        header_line = header_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        _log_stderr("_validate_header", "undecodable header line at {}: {}".format(file_path, exc))
        raise RulesParityError(
            "instruction file header is not valid UTF-8: {}".format(file_path),
            {"path": str(file_path)},
        ) from exc

    if dir_relative == "":
        shared_expected = "<!-- {} | {} -->".format(_AGENTS_FILENAME, _CLAUDE_FILENAME)
    else:
        shared_expected = "<!-- {}/{} | {} -->".format(dir_relative, _AGENTS_FILENAME, _CLAUDE_FILENAME)
    legacy_expected = "<!-- {} -->".format(repo_relative_of_file)

    if header_line != shared_expected and header_line != legacy_expected:
        _log_stderr(
            "_validate_header",
            "header {!r} at {} matches neither shared {!r} nor legacy {!r}".format(
                header_line, file_path, shared_expected, legacy_expected
            ),
        )
        raise RulesParityError(
            "instruction file header does not match the shared or legacy convention: {}".format(file_path),
            {
                "path": str(file_path),
                "header": header_line,
                "expectedShared": shared_expected,
                "expectedLegacy": legacy_expected,
            },
        )


def _walk_levels(repo_root: pathlib.Path, target: pathlib.Path) -> List[pathlib.Path]:
    """Build the root-first list of directory levels from ``repo_root`` down to ``target``.

    Raises RulesParityError when ``target`` is not under ``repo_root``.
    """
    try:
        relative = target.relative_to(repo_root)
    except ValueError as exc:
        _log_stderr("_walk_levels", "target {} is outside repo root {}".format(target, repo_root))
        raise RulesParityError(
            "target {} is outside repo root {}".format(target, repo_root),
            {"repoRoot": str(repo_root), "target": str(target)},
        ) from exc

    levels = [repo_root]
    current = repo_root
    for part in relative.parts:
        current = current / part
        levels.append(current)
    return levels


def _load_single_instruction(level_dir: pathlib.Path, resolved_root: pathlib.Path, filename: str) -> InstructionFile:
    """Load ONE instruction file at ``level_dir`` verbatim (permissive mode: no header/parity check).

    Standalone grok-skills default: a repo may carry only a CLAUDE.md (or only an
    AGENTS.md), and its header need not follow the optional strict path-header
    convention. The file's complete raw bytes (UTF-8 validated), its exact on-disk
    repo-relative path, and its SHA-256 are recorded as a single C7 block.
    """
    file_path = level_dir / filename
    content_bytes = _read_instruction_bytes(file_path)
    dir_relative = _posix_dir_relative(level_dir, resolved_root)
    repo_relative = _join_repo_relative(dir_relative, filename)
    return InstructionFile(
        path=file_path,
        repo_relative=repo_relative,
        content_bytes=content_bytes,
        sha256=hashlib.sha256(content_bytes).hexdigest(),
    )


def _discover_permissive(resolved_root: pathlib.Path, levels: List[pathlib.Path]) -> List[InstructionFile]:
    """Load whichever of AGENTS.md/CLAUDE.md exist at each level (AGENTS.md preferred when both).

    The zero-config, repo-agnostic default. At each level: load nothing when
    neither file exists; load the single present file when only one exists; when
    BOTH exist, load AGENTS.md as the representative block (the same representative
    strict mode uses, and the order the shared header lists) so identical pairs are
    not duplicated. No byte-parity or path-header enforcement -- that is the opt-in
    strict mode. A repo carrying only a CLAUDE.md (the common Claude Code case)
    loads that single file.
    """
    discovered: List[InstructionFile] = []
    for level_dir in levels:
        agents_name, claude_name = _scan_level(level_dir)
        if agents_name is not None:
            discovered.append(_load_single_instruction(level_dir, resolved_root, agents_name))
        elif claude_name is not None:
            discovered.append(_load_single_instruction(level_dir, resolved_root, claude_name))
    return discovered


def discover_instruction_files(
    repo_root: pathlib.Path, target: pathlib.Path, *, require_parity: bool = False
) -> "list[InstructionFile]":
    """Discover the C7 instruction-file chain for ``target``, root-first.

    Walks every directory level from ``repo_root`` down to ``target``
    (inclusive of both ends).

    Standalone grok-skills default (``require_parity=False``): loads whichever of
    AGENTS.md/CLAUDE.md exist at each level (CLAUDE.md preferred when both are
    present), verbatim, with no byte-parity or path-header enforcement, so a plain
    repo with a single rule file just works.

    Strict pair mode (``require_parity=True``, opt-in via ``.grok-skills.json``):
    at each level, if exactly one of the pair exists that is a RulesParityError;
    if both exist, their bodies (bytes after the first line) must be byte-identical
    and each file's own header line must match the shared-header or legacy
    per-file-header convention, else a RulesParityError. One InstructionFile per
    level is returned, using AGENTS.md as the canonical representative copy.

    Raises RulesParityError when ``target`` is not under ``repo_root``.
    """
    resolved_root = repo_root.resolve()
    resolved_target = target.resolve()
    levels = _walk_levels(resolved_root, resolved_target)

    if not require_parity:
        return _discover_permissive(resolved_root, levels)

    discovered: List[InstructionFile] = []
    for level_dir in levels:
        agents_name, claude_name = _scan_level(level_dir)
        if agents_name is None and claude_name is None:
            continue
        if agents_name is None or claude_name is None:
            present_name = _AGENTS_FILENAME if agents_name is not None else _CLAUDE_FILENAME
            missing_name = _CLAUDE_FILENAME if agents_name is not None else _AGENTS_FILENAME
            _log_stderr(
                "discover_instruction_files",
                "{} exists at {} without a matching {}".format(present_name, level_dir, missing_name),
            )
            raise RulesParityError(
                "{} exists at {} without a matching {}".format(present_name, level_dir, missing_name),
                {"level": str(level_dir), "present": present_name, "missing": missing_name},
            )

        agents_path = level_dir / agents_name
        claude_path = level_dir / claude_name
        agents_bytes = _read_instruction_bytes(agents_path)
        claude_bytes = _read_instruction_bytes(claude_path)

        agents_header, agents_body = _split_header(agents_bytes)
        claude_header, claude_body = _split_header(claude_bytes)

        if agents_body != claude_body:
            _log_stderr(
                "discover_instruction_files",
                "byte parity mismatch between {} and {}".format(agents_path, claude_path),
            )
            raise RulesParityError(
                "byte parity mismatch between {} and {}".format(agents_path, claude_path),
                {"agentsPath": str(agents_path), "claudePath": str(claude_path)},
            )

        dir_relative = _posix_dir_relative(level_dir, resolved_root)
        agents_repo_relative = _join_repo_relative(dir_relative, agents_name)
        claude_repo_relative = _join_repo_relative(dir_relative, claude_name)

        _validate_header(agents_header, dir_relative, agents_repo_relative, agents_path)
        _validate_header(claude_header, dir_relative, claude_repo_relative, claude_path)

        discovered.append(
            InstructionFile(
                path=agents_path,
                repo_relative=agents_repo_relative,
                content_bytes=agents_bytes,
                sha256=hashlib.sha256(agents_bytes).hexdigest(),
            )
        )

    return discovered


def _render_instruction_block(instruction: InstructionFile) -> str:
    body = instruction.content_bytes.decode("utf-8")
    if not body.endswith("\n"):
        body += "\n"
    return "--- BEGIN {0} ---\n{1}--- END {0} ---\n".format(instruction.repo_relative, body)


def build_prompt_payload(instructions: "list[InstructionFile]", task_text: str) -> str:
    """Render the exact C7 prompt template: rules banner, one BEGIN/END block per instruction, task.

    Trusts the caller-supplied order of ``instructions`` (root-first, as
    returned by ``discover_instruction_files``); this function does not
    re-sort.
    """
    blocks = "".join(_render_instruction_block(instruction) for instruction in instructions)
    return "{}\n{}{}\n{}".format(_RULES_BANNER, blocks, _TASK_BANNER, task_text)


def instruction_envelope_entries(instructions: "list[InstructionFile]") -> "list[dict]":
    """Build the C4 ``instructions[]`` envelope entries: repo-relative path, byte count, SHA-256."""
    return [
        {
            "path": instruction.repo_relative,
            "bytes": len(instruction.content_bytes),
            "sha256": instruction.sha256,
        }
        for instruction in instructions
    ]
