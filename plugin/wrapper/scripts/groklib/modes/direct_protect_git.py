# wrapper/scripts/groklib/modes/direct_protect_git.py
#
# Workspace gitdir discovery + logical-prefix resolution for direct_protect.
# Single source for nested/modules/in-workspace-gitfile inventory used by
# snapshot, restore, and git-dir guard. Keep snapshot/restore orchestration in
# direct_protect.py (900-line cap).

from __future__ import annotations

import os
import pathlib
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from groklib import GrokWrapperError, log_stderr

# Explicit sensitive file names under a gitdir (never walk objects).
_GIT_SNAPSHOT_FILES: Tuple[str, ...] = ("config", "HEAD", "packed-refs")
_GIT_SNAPSHOT_TREES: Tuple[str, ...] = ("hooks", "refs")

# Bound shared with the git-dir guard walk.
MAX_GIT_TREE_WALK_FILES = 20000
# Bound nested .git / gitfile discovery so ignored vendor caches cannot stall.
MAX_NESTED_GIT_DISCOVERY = 2000


def _log(function: str, message: str) -> None:
    log_stderr("modes.direct_protect_git", function, message)


def _posix_rel(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _is_snapshot_candidate(path: pathlib.Path) -> bool:
    """True for a regular file OR a symlink."""
    try:
        st = path.lstat()
    except OSError:
        return False
    import stat as _stat

    return _stat.S_ISREG(st.st_mode) or _stat.S_ISLNK(st.st_mode)


def _git_rel_parts(relative: str) -> Optional[Tuple[str, ...]]:
    """Return path parts under a ``.git`` segment, or None if not under any .git."""
    rel = _posix_rel(relative)
    if not rel:
        return None
    parts = tuple(p for p in rel.split("/") if p)
    try:
        idx = parts.index(".git")
    except ValueError:
        return None
    return parts[idx + 1 :]


def is_sensitive_git_relative(relative: str) -> bool:
    """True when ``relative`` is a guarded sensitive path under any workspace ``.git``.

    Covers root ``.git``, nested repo/submodule gitdirs (e.g. ``vendor/lib/.git``),
    and ``.git/modules/**`` (submodule metadata under the superproject). Sensitive
    set: config/HEAD/packed-refs, hooks/**, refs/**. Not sensitive: index,
    COMMIT_EDITMSG, objects/**, and other working-state metadata.
    """
    under = _git_rel_parts(relative)
    if under is None or not under:
        return False
    # Under .git/modules/<name>/... treat the remainder after modules/<name> as the
    # gitdir-relative path (and allow deeper modules nests).
    rest = under
    while len(rest) >= 2 and rest[0] == "modules":
        rest = rest[2:]
    if not rest:
        return False
    if rest[0] in _GIT_SNAPSHOT_FILES and len(rest) == 1:
        return True
    if rest[0] in _GIT_SNAPSHOT_TREES and len(rest) >= 2:
        return True
    return False


def is_snapshot_scope(relative: str) -> bool:
    """True when a protected path is in the pre-run snapshot set if it exists.

    Sensitive git metadata (any workspace ``.git`` / ``.git/modules/**``) plus
    non-git deny-glob matches (``.env``, keys, ...). ``.git/index`` /
    ``.git/COMMIT_EDITMSG`` and loose objects are not auto-restored when absent
    from the snapshot.
    """
    rel = _posix_rel(relative)
    if not rel:
        return False
    if is_sensitive_git_relative(rel):
        return True
    if rel == ".git" or "/.git/" in ("/" + rel + "/") or rel.startswith(".git/"):
        # Other .git/* is detect-only (deny matches) without snapshot auto-delete
        # unless it is in the sensitive set above.
        return False
    from groklib.modes.direct_finalize import path_matches_deny

    return path_matches_deny(rel)


def iter_git_tree_entries(
    git_dir: pathlib.Path,
    tree_name: str,
    *,
    rel_prefix: Optional[str] = None,
    max_files: int = MAX_GIT_TREE_WALK_FILES,
) -> Iterable[Tuple[str, pathlib.Path]]:
    """Yield ``(<rel_prefix>/<tree>/..., abs path)`` under ``git_dir/<tree>``.

    Single inventory for protected snapshot and git-dir guard: recursive,
    ``followlinks=False``, regular files and symlinks only, bounded by
    ``max_files``. ``git_dir`` may be the common dir of a linked worktree or a
    nested repo/module gitdir. ``rel_prefix`` defaults to ``.git``.
    """
    root = pathlib.Path(git_dir) / tree_name
    if not root.is_dir():
        return
    count = 0
    prefix = (rel_prefix or ".git").rstrip("/") + "/" + tree_name + "/"
    try:
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            dirnames.sort()
            for fname in sorted(filenames):
                child = pathlib.Path(dirpath) / fname
                if not _is_snapshot_candidate(child):
                    continue
                rel = prefix + _posix_rel(os.path.relpath(str(child), str(root)))
                yield rel, child
                count += 1
                if count >= max_files:
                    return
    except OSError as exc:
        _log(
            "iter_git_tree_entries",
            "{} walk failed: {}".format(tree_name, exc),
        )


def _read_gitfile_dir(gitfile: pathlib.Path) -> Optional[pathlib.Path]:
    """Parse a gitfile (``gitdir: <path>``) into an absolute path, or None."""
    try:
        text = gitfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith("gitdir:"):
            raw = stripped.split(":", 1)[1].strip()
            if not raw:
                return None
            p = pathlib.Path(raw)
            if not p.is_absolute():
                p = (gitfile.parent / p)
            try:
                return p.resolve()
            except OSError:
                return p
        break
    return None


def _is_within(child: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        child_r = child.resolve()
        root_r = root.resolve()
    except OSError:
        return False
    try:
        child_r.relative_to(root_r)
        return True
    except ValueError:
        return False


def discover_workspace_git_roots(
    repo_root: pathlib.Path,
    *,
    max_discovery: int = MAX_NESTED_GIT_DISCOVERY,
) -> List[Tuple[str, pathlib.Path]]:
    """Bounded no-symlink discovery of workspace gitdirs (root + nested + modules).

    Yields ``(repo_relative_git_prefix, abs_git_dir)`` for:
    - root ``.git`` directory
    - nested ``**/.git`` directories (vendored repos / plain submodules)
    - nested ``**/.git`` gitfiles whose ``gitdir:`` target resolves **inside**
      the workspace (honest linked-worktree limit: external common dirs skipped)
    - ``.git/modules/**`` directory trees under the root gitdir

    Fail closed with ``protected-path-write`` when discovery hits ``max_discovery``
    (unbounded ignored caches must not silently leave nested git unguarded).
    """
    root = pathlib.Path(repo_root)
    found: List[Tuple[str, pathlib.Path]] = []
    seen_abs: Set[str] = set()
    visits = 0

    def _add(rel_prefix: str, abs_dir: pathlib.Path) -> None:
        nonlocal visits
        visits += 1
        if visits > max_discovery:
            raise GrokWrapperError(
                "protected-path-write",
                "nested git discovery exceeded bound; fail closed rather than leave unguarded gitdirs",
                {
                    "maxDiscovery": max_discovery,
                    "hint": "reduce nested .git / vendor caches in the workspace or raise the bound only after review",
                },
            )
        try:
            key = str(abs_dir.resolve())
        except OSError:
            key = str(abs_dir)
        if key in seen_abs:
            return
        if not abs_dir.is_dir():
            return
        seen_abs.add(key)
        found.append((_posix_rel(rel_prefix).rstrip("/"), abs_dir))

    root_git = root / ".git"
    if root_git.is_dir() and not root_git.is_symlink():
        _add(".git", root_git)
        modules = root_git / "modules"
        if modules.is_dir() and not modules.is_symlink():
            # Each submodule dir under modules/ is itself a gitdir.
            try:
                for dirpath, dirnames, filenames in os.walk(str(modules), topdown=True, followlinks=False):
                    dirnames.sort()
                    # A modules entry looks like a gitdir when it has HEAD or config.
                    head = pathlib.Path(dirpath) / "HEAD"
                    config = pathlib.Path(dirpath) / "config"
                    if head.exists() or config.exists():
                        rel = _posix_rel(os.path.relpath(dirpath, str(root)))
                        _add(rel, pathlib.Path(dirpath))
                        # Still walk children (nested modules), but do not re-add via files.
                    visits += 1
                    if visits > max_discovery:
                        raise GrokWrapperError(
                            "protected-path-write",
                            "nested git discovery exceeded bound under .git/modules; fail closed",
                            {"maxDiscovery": max_discovery},
                        )
            except OSError as exc:
                _log("discover_workspace_git_roots", "modules walk failed: {}".format(exc))
    elif root_git.is_file() and not root_git.is_symlink():
        # Linked worktree gitfile at repo root: only protect when target is inside
        # the workspace (common dir often lives outside linked checkouts).
        target = _read_gitfile_dir(root_git)
        if target is not None and _is_within(target, root):
            _add(".git", target)

    # Nested .git dirs/files (vendored repos). Never follow symlinks; prune when
    # we enter a .git directory so we do not invent paths under objects/.
    try:
        for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
            visits += 1
            if visits > max_discovery:
                raise GrokWrapperError(
                    "protected-path-write",
                    "nested git discovery exceeded bound while scanning workspace; fail closed",
                    {"maxDiscovery": max_discovery},
                )
            rel_dir = _posix_rel(os.path.relpath(dirpath, str(root)))
            if rel_dir == ".":
                rel_dir = ""
            # Skip the root .git tree (handled above, including modules).
            if rel_dir == ".git" or rel_dir.startswith(".git/"):
                dirnames[:] = []
                continue
            # Prune symlink dirs always.
            keep = []
            for d in sorted(dirnames):
                child = pathlib.Path(dirpath) / d
                if child.is_symlink():
                    continue
                if d == ".git":
                    # Nested gitdir: add it, do not descend into objects/hooks here
                    # (inventory walks sensitive trees separately).
                    git_child = child
                    rel_git = (rel_dir + "/.git") if rel_dir else ".git"
                    if git_child.is_dir():
                        _add(rel_git, git_child)
                    continue
                keep.append(d)
            dirnames[:] = keep
            # Nested gitfiles named .git
            if ".git" in filenames:
                gitfile = pathlib.Path(dirpath) / ".git"
                if gitfile.is_file() and not gitfile.is_symlink():
                    target = _read_gitfile_dir(gitfile)
                    rel_git = (rel_dir + "/.git") if rel_dir else ".git"
                    if target is not None and _is_within(target, root):
                        _add(rel_git, target)
    except OSError as exc:
        _log("discover_workspace_git_roots", "workspace walk failed: {}".format(exc))
        raise GrokWrapperError(
            "protected-path-write",
            "nested git discovery failed; fail closed",
            {"error": str(exc)},
        ) from exc

    return found


def iter_sensitive_git_entries(
    repo_root: pathlib.Path,
    *,
    max_files_per_tree: int = MAX_GIT_TREE_WALK_FILES,
    git_roots: Optional[object] = None,
) -> Iterable[Tuple[str, pathlib.Path]]:
    """Yield ``(logical_rel, abs_path)`` for every sensitive file under workspace gitdirs.

    ``logical_rel`` uses the workspace ``.git`` prefix (e.g. ``.git/HEAD`` or
    ``vendor/lib/.git/hooks/x``) even when the actual bytes live in a gitfile
    target directory elsewhere in the workspace.

    When ``git_roots`` is provided (snapshot baseline), those prefix->abs mappings
    are used instead of live discovery so a post-run gitfile pointer rewrite
    cannot hide plants under the original common dir.
    """
    roots = _normalize_git_roots(git_roots)
    if not roots:
        roots = list(discover_workspace_git_roots(repo_root))
    for rel_prefix, git_dir in roots:
        for name in _GIT_SNAPSHOT_FILES:
            candidate = pathlib.Path(git_dir) / name
            if _is_snapshot_candidate(candidate):
                yield rel_prefix + "/" + name, candidate
        for tree in _GIT_SNAPSHOT_TREES:
            for rel, abs_path in iter_git_tree_entries(
                pathlib.Path(git_dir),
                tree,
                rel_prefix=rel_prefix,
                max_files=max_files_per_tree,
            ):
                yield rel, abs_path


def _normalize_git_roots(
    git_roots: Optional[object],
) -> List[Tuple[str, pathlib.Path]]:
    """Normalize git_roots from sequence pairs or prefix->abs dict to pairs."""
    if git_roots is None:
        return []
    if isinstance(git_roots, dict):
        return [
            (_posix_rel(str(prefix)).rstrip("/"), pathlib.Path(abs_dir))
            for prefix, abs_dir in git_roots.items()
            if _posix_rel(str(prefix)).rstrip("/")
        ]
    out: List[Tuple[str, pathlib.Path]] = []
    for item in git_roots:  # type: ignore[union-attr]
        if not item:
            continue
        prefix, abs_dir = item[0], item[1]
        pfx = _posix_rel(str(prefix)).rstrip("/")
        if pfx:
            out.append((pfx, pathlib.Path(abs_dir)))
    return out


def resolve_protected_abs_path(
    repo_root: pathlib.Path,
    relative: str,
    *,
    git_roots: Optional[object] = None,
) -> pathlib.Path:
    """Map a logical protected relative path to the actual absolute path.

    For free-standing ``.git`` directories this is ``repo_root / relative``.
    For in-workspace gitfiles the logical prefix (``.git`` or
    ``vendor/lib/.git``) maps onto the snapshotted/discovered absolute gitdir
    so restore never writes ``repo_root/.git/HEAD`` under a gitfile.

    Prefer a provided ``git_roots`` baseline (snapshot-time mapping). When
    omitted, live discovery is used only as a last resort.
    """
    root = pathlib.Path(repo_root)
    rel = _posix_rel(relative)
    if not rel:
        return root
    roots = _normalize_git_roots(git_roots)
    if not roots:
        roots = discover_workspace_git_roots(root)
    # Longest logical prefix wins (nested vendor/lib/.git before .git).
    for prefix, git_dir in sorted(roots, key=lambda item: len(item[0]), reverse=True):
        pfx = _posix_rel(prefix).rstrip("/")
        if not pfx:
            continue
        if rel == pfx:
            return pathlib.Path(git_dir)
        if rel.startswith(pfx + "/"):
            return pathlib.Path(git_dir) / rel[len(pfx) + 1 :]
    return root / rel
