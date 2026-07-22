# wrapper/scripts/groklib/modes/direct_protect_git.py
#
# Workspace gitdir discovery + logical-prefix resolution for direct_protect.
# Single source for nested/modules/in-workspace-gitfile inventory used by
# snapshot, restore, and git-dir guard. Sensitive-suffix classifier
# (is_sensitive_git_suffix / is_sensitive_git_relative) covers multi-component
# .git/modules/** paths; hooks/refs walks stream with no artificial file-count
# cap (real OSError fails closed). Keep snapshot/restore orchestration in
# direct_protect.py (900-line cap).

from __future__ import annotations

import os
import pathlib
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from groklib import GrokWrapperError, log_stderr

# Explicit sensitive file names under a gitdir (never walk objects).
_GIT_SNAPSHOT_FILES: Tuple[str, ...] = ("config", "HEAD", "packed-refs")
_GIT_SNAPSHOT_TREES: Tuple[str, ...] = ("hooks", "refs")

# Historical default for the retired hooks/refs file-count cap (kept as a
# named constant so tests can assert inventory past this bound still completes).
# Tree walks no longer truncate or fail-closed on file count; they stream.
MAX_GIT_TREE_WALK_FILES = 20000
# Max *gitdirs* (not workspace directories) to protect. 2000 was wrongly applied to
# every os.walk visit, so monorepos with >2k directories failed before any nested
# .git was found. Default is monorepo-safe; override via env.
_DEFAULT_MAX_NESTED_GIT_DISCOVERY = 50_000
_MIN_MAX_NESTED_GIT_DISCOVERY = 1
_MAX_MAX_NESTED_GIT_DISCOVERY = 500_000
# Separate anti-hang bound on os.walk directory visits (not gitdir count).
_DEFAULT_MAX_GIT_DISCOVERY_WALK_DIRS = 2_000_000
_MIN_MAX_GIT_DISCOVERY_WALK_DIRS = 10_000
_MAX_MAX_GIT_DISCOVERY_WALK_DIRS = 20_000_000
# Back-compat name: means max gitdirs (see nested_git_discovery_limit()).
MAX_NESTED_GIT_DISCOVERY = _DEFAULT_MAX_NESTED_GIT_DISCOVERY


def nested_git_discovery_limit() -> int:
    """Max nested/root gitdirs to inventory (SSOT; env override).

    Env: ``GROK_WRAPPER_MAX_NESTED_GIT_DISCOVERY`` (clamped). Counts **gitdirs**,
    not every workspace directory (monorepo fix).
    """
    raw = os.environ.get("GROK_WRAPPER_MAX_NESTED_GIT_DISCOVERY", "").strip()
    if raw:
        try:
            value = int(raw, 10)
        except ValueError:
            value = _DEFAULT_MAX_NESTED_GIT_DISCOVERY
    else:
        value = _DEFAULT_MAX_NESTED_GIT_DISCOVERY
    if value < _MIN_MAX_NESTED_GIT_DISCOVERY:
        return _MIN_MAX_NESTED_GIT_DISCOVERY
    if value > _MAX_MAX_NESTED_GIT_DISCOVERY:
        return _MAX_MAX_NESTED_GIT_DISCOVERY
    return value


def git_discovery_max_walk_dirs() -> int:
    """Max os.walk directory visits during nested-git scan (anti-hang).

    Env: ``GROK_WRAPPER_MAX_GIT_DISCOVERY_WALK_DIRS`` (clamped). Separate from
    gitdir discovery so monorepos with huge trees but few nested repos work.
    """
    raw = os.environ.get("GROK_WRAPPER_MAX_GIT_DISCOVERY_WALK_DIRS", "").strip()
    if raw:
        try:
            value = int(raw, 10)
        except ValueError:
            value = _DEFAULT_MAX_GIT_DISCOVERY_WALK_DIRS
    else:
        value = _DEFAULT_MAX_GIT_DISCOVERY_WALK_DIRS
    if value < _MIN_MAX_GIT_DISCOVERY_WALK_DIRS:
        return _MIN_MAX_GIT_DISCOVERY_WALK_DIRS
    if value > _MAX_MAX_GIT_DISCOVERY_WALK_DIRS:
        return _MAX_MAX_GIT_DISCOVERY_WALK_DIRS
    return value


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


def is_sensitive_git_suffix(parts: Sequence[str]) -> bool:
    """True when ``parts`` is a guarded sensitive path relative to a gitdir root.

    Single source for snapshot scope / restore / classifier: bare
    ``config`` / ``HEAD`` / ``packed-refs``, or ``hooks/**`` / ``refs/**``
    (at least one child under the tree). Ordinary metadata (``index``,
    ``objects/**``, ``COMMIT_EDITMSG``, ``logs/**``, ``description``) is not
    sensitive. Callers must supply the true gitdir-relative remainder (via a
    discovered ``git_roots`` prefix), not a guessed peel of ``modules/**``.
    """
    if not parts:
        return False
    head = parts[0]
    if head in _GIT_SNAPSHOT_FILES and len(parts) == 1:
        return True
    if head in _GIT_SNAPSHOT_TREES and len(parts) >= 2:
        return True
    return False


def is_sensitive_git_relative(
    relative: str,
    *,
    git_roots: Optional[object] = None,
) -> bool:
    """True when ``relative`` is a guarded sensitive path under a known gitdir.

    Authority is discovered / snapshotted ``git_roots`` (logical prefix -> abs
    gitdir), not token-name heuristics: module path components may legitimately
    be named ``hooks`` / ``refs`` / ``objects`` / ``logs``, so scanning for the
    first reserved token misclassifies ordinary module metadata.

    When ``git_roots`` is provided, the longest matching prefix wins and the
    remainder is classified with :func:`is_sensitive_git_suffix`. Without roots,
    only free-standing under-``.git`` paths (no ``modules/``) and the common
    single-component ``modules/<name>/...`` layout are classified (legacy tests /
    callers without discovery). Multi-component or reserved-name modules require
    ``git_roots``.

    Sensitive set: config/HEAD/packed-refs, hooks/**, refs/**. Not sensitive:
    index, COMMIT_EDITMSG, objects/**, logs/**, and other working-state metadata.
    """
    rel = _posix_rel(relative)
    if not rel:
        return False
    roots = _normalize_git_roots(git_roots)
    if roots:
        for prefix, _abs in sorted(roots, key=lambda item: len(item[0]), reverse=True):
            pfx = _posix_rel(str(prefix)).rstrip("/")
            if not pfx:
                continue
            if rel == pfx:
                # Bare logical gitdir key (marker or dir root) is not a file suffix.
                return False
            if rel.startswith(pfx + "/"):
                rest = rel[len(pfx) + 1 :]
                return is_sensitive_git_suffix(
                    tuple(p for p in rest.split("/") if p)
                )
        return False
    under = _git_rel_parts(rel)
    if under is None or not under:
        return False
    # Free-standing (root or nested vendor/.../.git without modules prefix).
    if under[0] != "modules":
        return is_sensitive_git_suffix(under)
    # Legacy single-component modules/<name>/... without discovery context.
    if len(under) >= 3:
        return is_sensitive_git_suffix(under[2:])
    return False


def is_snapshot_scope(
    relative: str,
    *,
    git_roots: Optional[object] = None,
) -> bool:
    """True when a protected path is in the pre-run snapshot set if it exists.

    Sensitive git metadata (any workspace ``.git`` / ``.git/modules/**`` under
    known ``git_roots``) plus non-git deny-glob matches (``.env``, keys, ...).
    ``.git/index`` / ``.git/COMMIT_EDITMSG`` and loose objects are not
    auto-restored when absent from the snapshot.
    """
    rel = _posix_rel(relative)
    if not rel:
        return False
    if is_sensitive_git_relative(rel, git_roots=git_roots):
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
) -> Iterable[Tuple[str, pathlib.Path]]:
    """Yield ``(<rel_prefix>/<tree>/..., abs path)`` under ``git_dir/<tree>``.

    Single inventory for protected snapshot and git-dir guard: recursive,
    ``followlinks=False``, regular files and symlinks only. Streams the full
    tree with no artificial file-count cap (trusted repo input; hooks/refs must
    be complete). Real ``OSError`` during walk is logged; callers that need
    fail-closed on resource errors should re-walk with stricter policy.
    ``git_dir`` may be the common dir of a linked worktree or a nested
    repo/module gitdir. ``rel_prefix`` defaults to ``.git``.
    """
    root = pathlib.Path(git_dir) / tree_name
    if not root.is_dir():
        return
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
    except OSError as exc:
        _log(
            "iter_git_tree_entries",
            "{} walk failed: {}".format(tree_name, exc),
        )
        raise GrokWrapperError(
            "protected-path-write",
            "git tree walk failed under {}/{}; fail closed".format(
                (rel_prefix or ".git").rstrip("/"), tree_name
            ),
            {"tree": tree_name, "error": str(exc)},
        ) from exc


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


def _inventory_modules_under(
    abs_git_dir: pathlib.Path,
    rel_prefix: str,
    *,
    add_fn,
    max_discovery: int,
    gitdir_count: List[int],
    walk_count: List[int],
    max_walk_dirs: int,
) -> None:
    """Discover ``modules/**`` gitdirs under one abs gitdir (bounded, no symlink).

    Logical keys are ``{rel_prefix}/modules/<name>/...``. Called for every
    discovered free-standing or gitfile-target gitdir so root-gitfile common
    dirs and nested repos are covered - not only free-standing root ``.git``.
    """
    modules = pathlib.Path(abs_git_dir) / "modules"
    if not modules.is_dir() or modules.is_symlink():
        return
    pfx = _posix_rel(rel_prefix).rstrip("/")
    try:
        for dirpath, dirnames, _filenames in os.walk(
            str(modules), topdown=True, followlinks=False
        ):
            walk_count[0] += 1
            if walk_count[0] > max_walk_dirs:
                raise GrokWrapperError(
                    "protected-path-write",
                    "nested git modules walk exceeded directory visit bound under {}/modules".format(
                        pfx
                    ),
                    {
                        "maxWalkDirs": max_walk_dirs,
                        "hint": "raise GROK_WRAPPER_MAX_GIT_DISCOVERY_WALK_DIRS if this is a real monorepo",
                    },
                )
            dirnames.sort()
            # Do not follow symlink children inside modules.
            dirnames[:] = [
                d
                for d in dirnames
                if not (pathlib.Path(dirpath) / d).is_symlink()
            ]
            head = pathlib.Path(dirpath) / "HEAD"
            config = pathlib.Path(dirpath) / "config"
            if head.exists() or config.exists():
                rel_mod = _posix_rel(os.path.relpath(dirpath, str(modules)))
                if rel_mod != ".":
                    logical = pfx + "/modules/" + rel_mod
                    add_fn(logical, pathlib.Path(dirpath))
                    # Nested modules/ of this gitdir are inventoried by add_fn;
                    # do not walk objects/hooks/refs here (duplicate + unbounded).
                    dirnames[:] = []
    except OSError as exc:
        _log(
            "_inventory_modules_under",
            "modules walk failed for {}: {}".format(abs_git_dir, exc),
        )


def discover_workspace_git_roots(
    repo_root: pathlib.Path,
    *,
    max_discovery: Optional[int] = None,
    max_walk_dirs: Optional[int] = None,
) -> List[Tuple[str, pathlib.Path]]:
    """Bounded no-symlink discovery of workspace gitdirs (root + nested + modules).

    Yields ``(repo_relative_git_prefix, abs_git_dir)`` for:
    - root ``.git`` directory or in-workspace gitfile target
    - nested ``**/.git`` directories (vendored repos / plain submodules)
    - nested ``**/.git`` gitfiles whose ``gitdir:`` target resolves **inside**
      the workspace (honest linked-worktree limit: external common dirs skipped)
    - ``modules/**`` under **every** discovered abs gitdir (root free-standing,
      root gitfile target, nested free-standing, nested gitfile target)

    ``max_discovery`` bounds **gitdirs found** (not every workspace directory).
    ``max_walk_dirs`` bounds os.walk visits (monorepo-scale anti-hang). Both
    fail closed with ``protected-path-write`` rather than silently skip.
    ``seen_abs`` prevents recursive duplicate loops when the same gitdir is
    reachable via multiple logical prefixes.
    """
    if max_discovery is None:
        max_discovery = nested_git_discovery_limit()
    if max_walk_dirs is None:
        max_walk_dirs = git_discovery_max_walk_dirs()
    root = pathlib.Path(repo_root)
    found: List[Tuple[str, pathlib.Path]] = []
    seen_abs: Set[str] = set()
    gitdir_count = [0]
    walk_count = [0]

    def _add(rel_prefix: str, abs_dir: pathlib.Path, *, inventory_modules: bool = True) -> None:
        # Count gitdirs only (not ordinary workspace dirs).
        gitdir_count[0] += 1
        if gitdir_count[0] > max_discovery:
            raise GrokWrapperError(
                "protected-path-write",
                "nested git discovery exceeded gitdir bound; fail closed rather than leave unguarded gitdirs",
                {
                    "maxDiscovery": max_discovery,
                    "hint": (
                        "raise GROK_WRAPPER_MAX_NESTED_GIT_DISCOVERY "
                        "(counts nested/.git modules, not every monorepo directory)"
                    ),
                },
            )
        if not abs_dir.is_dir():
            return
        try:
            key = str(abs_dir.resolve())
        except OSError:
            key = str(abs_dir)
        pfx = _posix_rel(rel_prefix).rstrip("/")
        if not pfx:
            return
        # Always retain the logical prefix mapping. seen_abs only suppresses
        # modules/** recursion so a real submodule alias
        # (vendor/lib/.git gitfile -> .git/modules/lib) still gets its own
        # logical prefix even when the abs gitdir was already inventoried.
        already = key in seen_abs
        if not already:
            seen_abs.add(key)
        found_keys: Set[Tuple[str, str]] = set()
        for ep, ea in found:
            try:
                found_keys.add((_posix_rel(ep).rstrip("/"), str(ea.resolve())))
            except OSError:
                found_keys.add((_posix_rel(ep).rstrip("/"), str(ea)))
        if (pfx, key) not in found_keys:
            found.append((pfx, abs_dir))
        if inventory_modules and not already:
            # Modules under this gitdir once per abs only (seen_abs recursion guard).
            _inventory_modules_under(
                abs_dir,
                pfx,
                add_fn=lambda r, a: _add(r, a, inventory_modules=True),
                max_discovery=max_discovery,
                gitdir_count=gitdir_count,
                walk_count=walk_count,
                max_walk_dirs=max_walk_dirs,
            )

    root_git = root / ".git"
    if root_git.is_dir() and not root_git.is_symlink():
        _add(".git", root_git)
    elif root_git.is_file() and not root_git.is_symlink():
        # Linked worktree gitfile at repo root: only protect when target is inside
        # the workspace (common dir often lives outside linked checkouts).
        target = _read_gitfile_dir(root_git)
        if target is not None and _is_within(target, root):
            _add(".git", target)

    # Nested .git dirs/files (vendored repos). Never follow symlinks; prune when
    # we enter a .git directory so we do not invent paths under objects/.
    # Walk-dir budget is separate from gitdir discovery (monorepo-safe).
    try:
        for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
            walk_count[0] += 1
            if walk_count[0] > max_walk_dirs:
                raise GrokWrapperError(
                    "protected-path-write",
                    "nested git discovery walk exceeded directory visit bound; fail closed",
                    {
                        "maxWalkDirs": max_walk_dirs,
                        "hint": "raise GROK_WRAPPER_MAX_GIT_DISCOVERY_WALK_DIRS for huge trees",
                    },
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


def discover_workspace_gitfiles(
    repo_root: pathlib.Path,
) -> List[Tuple[str, pathlib.Path]]:
    """Yield ``(logical_prefix, gitfile_abs)`` for in-workspace ``.git`` files.

    Used by the git-dir guard to fingerprint pointer content so an external or
    in-workspace redirect is not silent. Does not follow the target.
    """
    root = pathlib.Path(repo_root)
    out: List[Tuple[str, pathlib.Path]] = []
    root_git = root / ".git"
    if root_git.is_file() and not root_git.is_symlink():
        out.append((".git", root_git))
    try:
        for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
            rel_dir = _posix_rel(os.path.relpath(dirpath, str(root)))
            if rel_dir == ".":
                rel_dir = ""
            if rel_dir == ".git" or rel_dir.startswith(".git/"):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d
                for d in sorted(dirnames)
                if d != ".git" and not (pathlib.Path(dirpath) / d).is_symlink()
            ]
            if ".git" in filenames:
                gitfile = pathlib.Path(dirpath) / ".git"
                if gitfile.is_file() and not gitfile.is_symlink():
                    rel = (rel_dir + "/.git") if rel_dir else ".git"
                    out.append((_posix_rel(rel), gitfile))
    except OSError as exc:
        _log("discover_workspace_gitfiles", "walk failed: {}".format(exc))
    return out


def merge_git_root_pairs(
    *groups: Optional[object],
) -> List[Tuple[str, pathlib.Path]]:
    """Union multiple git-root maps/lists; keep ALL (prefix, abs) pairs.

    Same logical prefix may map to multiple abs dirs (baseline common + live
    redirect target). Detection fingerprints both; restore still prefers
    baseline-first single mapping via ``resolve_protected_abs_path``.
    """
    out: List[Tuple[str, pathlib.Path]] = []
    seen: Set[Tuple[str, str]] = set()
    for group in groups:
        for pfx, abs_dir in _normalize_git_roots(group):
            try:
                key = (pfx, str(pathlib.Path(abs_dir).resolve()))
            except OSError:
                key = (pfx, str(abs_dir))
            if key in seen:
                continue
            seen.add(key)
            out.append((pfx, pathlib.Path(abs_dir)))
    return out


def iter_sensitive_git_entries(
    repo_root: pathlib.Path,
    *,
    git_roots: Optional[object] = None,
    also_live: bool = False,
) -> Iterable[Tuple[str, pathlib.Path]]:
    """Yield ``(logical_rel, abs_path)`` for every sensitive file under workspace gitdirs.

    ``logical_rel`` uses the workspace ``.git`` prefix (e.g. ``.git/HEAD`` or
    ``vendor/lib/.git/hooks/x``) even when the actual bytes live in a gitfile
    target directory elsewhere in the workspace.

    When ``git_roots`` is provided (snapshot baseline), those prefix->abs mappings
    are used. With ``also_live=True``, live discovery is **unioned** so a post-run
    in-workspace pointer redirect's new-side plants are still fingerprinted, while
    baseline abs paths remain for original common continuity. Hooks/refs walks
    stream without an artificial file-count cap.
    """
    roots = _normalize_git_roots(git_roots)
    if also_live or not roots:
        live = list(discover_workspace_git_roots(repo_root))
        roots = merge_git_root_pairs(roots, live) if roots else live
    # Emit sensitive entries under EVERY logical prefix, including submodule
    # aliases (vendor/lib/.git and .git/modules/lib share an abs gitdir).
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
    abs_paths: Optional[Dict[str, str]] = None,
) -> pathlib.Path:
    """Map a logical protected relative path to the actual absolute path.

    Order (restore contract):
    1. ``abs_paths[rel]`` exact snapshot map (gitfile marker bytes, sensitive files)
    2. longest ``git_roots`` prefix for children under a logical ``.git``
       (``vendor/lib/.git/HEAD`` -> modules abs + HEAD) - never for the bare
       marker key itself (``.git`` / ``vendor/lib/.git`` stay the gitfile path)
    3. live discovery when no baseline provided
    4. ``repo_root / relative`` fallback

    Never map a gitfile marker key to its target gitdir (that would replace a
    directory with a file or trash a redirect target).
    """
    root = pathlib.Path(repo_root)
    rel = _posix_rel(relative)
    if not rel:
        return root
    if abs_paths and rel in abs_paths:
        return pathlib.Path(abs_paths[rel])
    roots = _normalize_git_roots(git_roots)
    if not roots:
        roots = discover_workspace_git_roots(root)
    # Longest logical prefix wins for *children* only (not the bare marker key).
    for prefix, git_dir in sorted(roots, key=lambda item: len(item[0]), reverse=True):
        pfx = _posix_rel(prefix).rstrip("/")
        if not pfx:
            continue
        if rel == pfx:
            # Bare git root key: free-standing dir OR gitfile marker path under
            # the workspace - never the modules target for a gitfile alias.
            return root / rel
        if rel.startswith(pfx + "/"):
            return pathlib.Path(git_dir) / rel[len(pfx) + 1 :]
    return root / rel
