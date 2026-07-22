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

import os
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


def _filter_actually_ignored(
    repo: pathlib.Path,
    candidates: List[str],
    *,
    error_class: str,
) -> List[str]:
    """Keep only paths that ``git check-ignore`` classifies as ignored.

    ``ls-files --others --ignored --directory`` may also emit untracked parent
    directories that merely *contain* ignored children (e.g. ``other/`` when only
    ``other/__pycache__/`` is ignored). Those parents are NOT ignored and must
    not enter the ignored inventory (write-scope / dirty-overlap byproduct filter
    would otherwise see a false source path). Fully ignored trees such as
    ``node_modules/`` still pass check-ignore.
    """
    if not candidates:
        return []
    # Batch check-ignore -z stdin; exit 1 means none ignored.
    try:
        payload = b"\0".join(
            entry.encode("utf-8", errors="surrogateescape") for entry in candidates
        ) + b"\0"
    except (UnicodeEncodeError, UnicodeError) as exc:
        _log("_filter_actually_ignored", "path encode failed: {}".format(exc))
        raise GrokWrapperError(
            error_class,
            "could not encode ignored-path candidates for git check-ignore: {}".format(exc),
            {"error": str(exc)},
        ) from exc
    args = ("check-ignore", "--stdin", "-z")
    completed = worktree._run_git_bytes(
        repo,
        args,
        env=worktree._git_env(),
        input_bytes=payload,
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        _log(
            "_filter_actually_ignored",
            "git check-ignore exited {}: {}".format(
                completed.returncode, _stderr_text(completed)
            ),
        )
        raise GrokWrapperError(
            error_class,
            "git check-ignore failed while filtering ignored-path inventory",
            {
                "exitStatus": completed.returncode,
                "stderr": _stderr_text(completed),
            },
        )
    kept_raw = decode_nul_paths(completed.stdout or b"")
    kept: set = set()
    for entry in kept_raw:
        kept.add(entry)
        kept.add(entry.rstrip("/"))
        if not entry.endswith("/"):
            kept.add(entry + "/")
    # Preserve candidate order for stable fingerprints / diffs.
    return [
        entry
        for entry in candidates
        if entry in kept or entry.rstrip("/") in kept or (entry + "/") in kept
    ]


def _expand_protected_leaves_under_ignored_dirs(
    repo: pathlib.Path,
    candidates: List[str],
) -> List[str]:
    """Add deny-scoped leaves under collapsed ignored directories.

    ``ls-files --directory`` reports fully ignored trees as a single entry
    (e.g. ``secrets/``). Direct-mode deny scans match globs against those
    inventory paths, so a planted ``secrets/id_rsa`` would not match ``id_rsa``
    / ``*.pem`` if only the directory token is present (Codex PR #9). Walk each
    collapsed directory and append paths that match the deny-write SSOT; bulk
    caches without deny leaves stay collapsed (issue #7).
    """
    # Late import keeps path_inventory free of deny_write at module load for
    # callers that only need NUL split / working-tree listings.
    from groklib.deny_write import path_matches_deny

    root = pathlib.Path(repo)
    out: List[str] = list(candidates)
    seen = set(candidates)
    # Also index slash-stripped forms so we do not double-add.
    for entry in candidates:
        seen.add(entry.rstrip("/"))
        if not entry.endswith("/"):
            seen.add(entry + "/")

    for entry in candidates:
        rel_dir = entry.rstrip("/")
        abs_dir = root / rel_dir
        try:
            if not abs_dir.is_dir() or abs_dir.is_symlink():
                continue
        except OSError:
            continue
        # Directory entries from --directory often end with "/"; plain files do not.
        # Still walk any candidate that is a real directory on disk.
        try:
            for dirpath, dirnames, filenames in os.walk(
                str(abs_dir), topdown=True, followlinks=False
            ):
                # Never descend into nested .git (deny covers .git/* via other guards).
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d != ".git" and not (pathlib.Path(dirpath) / d).is_symlink()
                ]
                for name in filenames:
                    abs_file = pathlib.Path(dirpath) / name
                    # Include regular files and symlink leaves (planted key links).
                    try:
                        if not abs_file.is_file() and not abs_file.is_symlink():
                            continue
                    except OSError:
                        continue
                    try:
                        rel = abs_file.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if not path_matches_deny(rel):
                        continue
                    if rel not in seen:
                        seen.add(rel)
                        out.append(rel)
        except OSError as exc:
            _log(
                "_expand_protected_leaves_under_ignored_dirs",
                "walk failed under {}: {}".format(rel_dir, exc),
            )
    return out


def list_ignored_untracked_paths(
    repo: pathlib.Path,
    *,
    error_class: str = "worktree-failure",
) -> List[str]:
    """Ignored untracked repo-relative paths (collapsed + check-ignore filtered).

    Uses ``--directory`` so fully ignored directories collapse to a single entry
    (e.g. ``node_modules/`` instead of every file under it). Without that flag,
    large monorepos can spend tens of seconds enumerating ignored trees and
    trip the shared 30s git timeout (GitHub issue #7), blocking worktree-based
    ``code --integration review|auto`` before Grok starts.

    Results are filtered with ``git check-ignore`` so untracked parents that only
    contain ignored children are dropped (not themselves ignored).

    Deny-scoped leaves under collapsed ignored directories (e.g. ``secrets/id_rsa``
    when ``secrets/`` is ignored) are expanded back into the inventory so
    direct-mode protected-path scans still match basename globs (Codex PR #9).
    Non-deny leaves under bulk caches stay collapsed.

    File-level ignore patterns still list individual files. Escape fingerprinting
    already treats bulk ignored caches as stat-only; directory collapse matches
    that bulk semantic while remaining sensitive to newly planted top-level
    ignored paths and new ignored trees.
    """
    candidates = [
        entry
        for entry in list_ls_files(
            repo,
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            error_class=error_class,
        )
        if entry
    ]
    filtered = _filter_actually_ignored(repo, candidates, error_class=error_class)
    return _expand_protected_leaves_under_ignored_dirs(repo, filtered)
