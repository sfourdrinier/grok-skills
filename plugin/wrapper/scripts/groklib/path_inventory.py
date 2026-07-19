# wrapper/scripts/groklib/path_inventory.py
#
# Single NUL-safe source of truth for repo-relative path inventories from git.
# Non -z path listers C-quote non-ASCII names under default core.quotePath
# (e.g. café.txt -> "caf\303\251.txt"). Callers must use these helpers (raw -z
# via the shared worktree bytes git runner + NUL split) and never C-unquote
# already-raw -z paths. Consumed by worktree_escape fingerprinting/escape
# checks, worktree diff_summary / tree-to-tree listings, and handoff_patch
# changed-path lists.

from __future__ import annotations

import pathlib
from typing import List, Union

from groklib import GrokWrapperError, log_stderr
from groklib import worktree


def _log(function: str, message: str) -> None:
    log_stderr("path_inventory", function, message)


def decode_nul_paths(payload: Union[bytes, str]) -> List[str]:
    """Decode a NUL-separated path inventory; never C-unquote.

    ``git ... -z`` already emits raw path bytes/text. Bytes use surrogateescape
    so non-UTF-8 pathnames survive fingerprinting; str payloads (legacy text
    runners) are split on NUL only and cannot recover destroyed bytes.
    """
    if not payload:
        return []
    if isinstance(payload, bytes):
        parts = payload.split(b"\0")
        return [
            raw.decode("utf-8", errors="surrogateescape") for raw in parts if raw
        ]
    return [entry for entry in payload.split("\0") if entry]


def _stderr_text(completed) -> str:
    raw = completed.stderr
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw).strip()


def _run_inventory_git(repo: pathlib.Path, args: tuple) -> "object":
    """Run inventory git via the shared bytes runner (surrogateescape-ready)."""
    return worktree._run_git_bytes(repo, args)


def list_diff_name_only(
    repo: pathlib.Path,
    *revisions: str,
    error_class: str = "worktree-failure",
) -> List[str]:
    """Return repo-relative paths from ``git diff --name-only -z`` (exit 0 or 1).

    Accepts zero, one, or two revision args (working-tree vs base, or tree-to-tree).
    Non-(0,1) exits fail closed so inventories never silently shrink. Git is
    invoked in bytes mode; NUL fields decode via utf-8 surrogateescape.
    """
    args = ("diff", "--name-only", "-z", *revisions)
    completed = _run_inventory_git(repo, args)
    if completed.returncode not in (0, 1):
        _log(
            "list_diff_name_only",
            "git diff --name-only -z exited {}: {}".format(
                completed.returncode, _stderr_text(completed)
            ),
        )
        raise GrokWrapperError(
            error_class,
            "git diff --name-only failed while listing paths",
            {
                "exitStatus": completed.returncode,
                "stderr": _stderr_text(completed),
                "argv": ["git", "-C", str(repo)] + list(args),
            },
        )
    return decode_nul_paths(completed.stdout or b"")


def list_ls_files(
    repo: pathlib.Path,
    *extra_args: str,
    error_class: str = "worktree-failure",
) -> List[str]:
    """Return repo-relative paths from ``git ls-files -z`` plus ``extra_args``."""
    args = ("ls-files", "-z", *extra_args)
    completed = _run_inventory_git(repo, args)
    if completed.returncode != 0:
        _log(
            "list_ls_files",
            "git ls-files -z exited {}: {}".format(
                completed.returncode, _stderr_text(completed)
            ),
        )
        raise GrokWrapperError(
            error_class,
            "git ls-files failed while listing paths",
            {
                "exitStatus": completed.returncode,
                "stderr": _stderr_text(completed),
                "argv": ["git", "-C", str(repo)] + list(args),
            },
        )
    return decode_nul_paths(completed.stdout or b"")


def list_working_tree_changed_paths(
    repo: pathlib.Path,
    base_revision: str = "HEAD",
    *,
    error_class: str = "worktree-failure",
) -> List[str]:
    """Tracked changes vs ``base_revision`` plus untracked non-ignored paths."""
    tracked = list_diff_name_only(repo, base_revision, error_class=error_class)
    untracked = list_ls_files(
        repo, "--others", "--exclude-standard", error_class=error_class
    )
    return sorted({entry for entry in (tracked + untracked) if entry})


def list_ignored_untracked_paths(
    repo: pathlib.Path,
    *,
    error_class: str = "worktree-failure",
) -> List[str]:
    """Every IGNORED untracked repo-relative path (one ``ls-files`` call)."""
    return [
        entry
        for entry in list_ls_files(
            repo,
            "--others",
            "--ignored",
            "--exclude-standard",
            error_class=error_class,
        )
        if entry
    ]
