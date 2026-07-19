# wrapper/scripts/groklib/modes/direct_protect.py
#
# Pre-run snapshot + post-run restore of protected paths for hardened-direct.
#
# SECURITY HONESTY:
#   - Direct mode does NOT prevent protected writes at the sandbox layer
#     (workspace profile is whole-root writable). This module rolls back the
#     COVERED protected set after a run (byte-identical if snapshotted; removed
#     if Grok created it): .env/keys plus git metadata for the root .git,
#     nested workspace gitdirs/gitfiles, and .git/modules/** sensitive trees
#     (config/HEAD/packed-refs, hooks/**, refs/**). .git/index and
#     .git/COMMIT_EDITMSG are NOT guarded (benign working state git rewrites on
#     ordinary reads); loose .git/objects are not tracked (inert until a watched
#     ref points at them).
#   - Linked worktree ``.git`` files (gitfile) are discovered; sensitive paths
#     under the pointed-to common/per-worktree dir are protected only when that
#     dir is inside the workspace. Out-of-workspace common dirs are not walked.
#   - It does NOT protect against reads (documented D-SECRETREAD gap: Grok can
#     still read .env / keys inside the repo).
#   - Over-cap protected files / discovery overflow fail closed with honest
#     messages rather than claiming full coverage or restore.
#   - Backlog: probe seatbelt write-deny subpaths for true prevention.

import dataclasses
import json
import os
import pathlib
import shutil
import stat
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from groklib import GrokWrapperError, log_stderr

SNAPSHOT_DIR_NAME = "protected-snapshot"
MANIFEST_NAME = "manifest.json"
# Bound total snapshot payload so a huge .pem cannot fill the run dir.
DEFAULT_MAX_TOTAL_BYTES = 25 * 1024 * 1024

# Explicit sensitive file names under a gitdir (never walk objects: loose objects
# are content-addressed and inert until a ref points at them; refs/** and hooks/**
# ARE snapshotted so a ref/hook move or plant can be rolled back).
_GIT_SNAPSHOT_FILES: Tuple[str, ...] = ("config", "HEAD", "packed-refs")
_GIT_SNAPSHOT_TREES: Tuple[str, ...] = ("hooks", "refs")

# Bound shared with the git-dir guard walk so a pathological hooks/refs tree
# cannot stall snapshot or finalize (same cap for both inventories).
MAX_GIT_TREE_WALK_FILES = 20000
# Bound nested .git / gitfile discovery so ignored vendor caches cannot stall.
MAX_NESTED_GIT_DISCOVERY = 2000


def _log(function: str, message: str) -> None:
    log_stderr("modes.direct_protect", function, message)


@dataclasses.dataclass(frozen=True)
class ProtectedPathEntry:
    """One pre-run protected path record."""

    relative: str
    existed: bool
    snapshotted: bool
    size: int
    reason: Optional[str] = None  # e.g. "over-cap"
    mode: int = 0  # pre-run permission bits (reapplied on restore of a regular file)
    symlink_target: Optional[str] = None  # set when the pre-run path was a symlink


@dataclasses.dataclass(frozen=True)
class ProtectedSnapshot:
    """Pre-run protected-path index + on-disk byte copies under the run dir.

    ``abs_paths`` maps logical repo-relative keys (e.g. ``.git/HEAD``) to the
    actual absolute filesystem path snapshotted. Required when ``.git`` is a
    gitfile pointing at an in-workspace common/per-worktree dir: restore must
    write the real gitdir, never ``repo_root/.git/<child>`` under the gitfile.
    """

    run_dir: pathlib.Path
    snapshot_dir: pathlib.Path
    entries: Dict[str, ProtectedPathEntry]
    total_bytes: int
    max_total_bytes: int
    abs_paths: Dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class RestoreResult:
    """Outcome of rolling back protected offenders."""

    restored: List[str]
    unrestored: List[str]
    errors: List[Dict[str, str]]
    honest_message: Optional[str] = None


def _posix_rel(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _mkdir_0700(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path), 0o700)
    except OSError as exc:
        _log("_mkdir_0700", "chmod 0700 failed for {}: {}".format(path, exc))


def _chmod_0600(path: pathlib.Path) -> None:
    try:
        os.chmod(str(path), 0o600)
    except OSError as exc:
        _log("_chmod_0600", "chmod 0600 failed for {}: {}".format(path, exc))


def _is_regular_file(path: pathlib.Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _is_snapshot_candidate(path: pathlib.Path) -> bool:
    """True for a regular file OR a symlink (a pre-existing protected symlink must
    be snapshotted so restore recreates it instead of deleting it - review)."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)


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
) -> Iterable[Tuple[str, pathlib.Path]]:
    """Yield ``(logical_rel, abs_path)`` for every sensitive file under workspace gitdirs.

    ``logical_rel`` uses the workspace ``.git`` prefix (e.g. ``.git/HEAD`` or
    ``vendor/lib/.git/hooks/x``) even when the actual bytes live in a gitfile
    target directory elsewhere in the workspace.
    """
    for rel_prefix, git_dir in discover_workspace_git_roots(repo_root):
        for name in _GIT_SNAPSHOT_FILES:
            candidate = git_dir / name
            if _is_snapshot_candidate(candidate):
                yield rel_prefix + "/" + name, candidate
        for tree in _GIT_SNAPSHOT_TREES:
            for rel, abs_path in iter_git_tree_entries(
                git_dir, tree, rel_prefix=rel_prefix, max_files=max_files_per_tree
            ):
                yield rel, abs_path


def resolve_protected_abs_path(
    repo_root: pathlib.Path,
    relative: str,
    *,
    git_roots: Optional[Sequence[Tuple[str, pathlib.Path]]] = None,
) -> pathlib.Path:
    """Map a logical protected relative path to the actual absolute path.

    For free-standing ``.git`` directories this is ``repo_root / relative``.
    For in-workspace gitfiles the logical prefix (``.git`` or
    ``vendor/lib/.git``) maps onto the discovered absolute gitdir so restore
    never writes ``repo_root/.git/HEAD`` under a gitfile.
    """
    root = pathlib.Path(repo_root)
    rel = _posix_rel(relative)
    if not rel:
        return root
    roots = list(git_roots) if git_roots is not None else discover_workspace_git_roots(root)
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


def iter_existing_protected_paths(repo_root: pathlib.Path) -> Iterable[str]:
    """Yield logical repo-relative POSIX paths of existing protected files."""
    for rel, _abs in iter_existing_protected_path_map(repo_root).items():
        yield rel


def iter_existing_protected_path_map(
    repo_root: pathlib.Path,
) -> Dict[str, pathlib.Path]:
    """Map logical protected relative path -> actual absolute path to snapshot.

    Sensitive git metadata uses discovered gitdir abs paths (gitfile-safe).
    Other deny-glob matches resolve under ``repo_root``. Does not walk
    ``objects``. Prunes ``.git`` directory names during the deny walk.
    """
    # Late import: direct_finalize imports this module for restore.
    from groklib.modes.direct_finalize import path_matches_deny

    root = pathlib.Path(repo_root)
    mapping: Dict[str, pathlib.Path] = {}
    git_root_prefixes = {
        _posix_rel(prefix).rstrip("/")
        for prefix, _git_dir in discover_workspace_git_roots(root)
        if _posix_rel(prefix).rstrip("/")
    }
    for rel, abs_path in iter_sensitive_git_entries(root):
        mapping[rel] = abs_path

    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        rel_dir = _posix_rel(os.path.relpath(dirpath, str(root)))
        if rel_dir == ".":
            rel_dir = ""
        # Never descend into any .git (sensitive subset already mapped).
        dirnames[:] = [
            d
            for d in dirnames
            if d != ".git" and not (pathlib.Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            if rel_dir:
                rel = rel_dir + "/" + name
            else:
                rel = name
            rel = _posix_rel(rel)
            if not path_matches_deny(rel):
                continue
            if is_sensitive_git_relative(rel):
                continue
            if rel in mapping:
                continue
            # Do not snapshot the gitfile/gitdir marker itself under a key that
            # collides with sensitive children (``.git`` / ``vendor/.../.git``):
            # contents are inventoried via discover + iter_sensitive_git_entries.
            if rel in git_root_prefixes:
                continue
            candidate = root / rel
            if _is_snapshot_candidate(candidate):
                mapping[rel] = candidate
    return mapping


def snapshot_protected_paths(
    repo_root: pathlib.Path,
    run_dir: pathlib.Path,
    *,
    max_total_bytes: Optional[int] = None,
) -> ProtectedSnapshot:
    """Copy pre-run protected file bytes under ``run_dir/protected-snapshot/``.

    Directory mode 0700; snapshot files 0600. Paths larger than the remaining
    budget are recorded as unsnapshottable (``snapshotted=False``, reason
    ``over-cap``) without copying. Sensitive git paths are read from the
    **actual** gitdir (gitfile targets included), while the snapshot store key
    remains the logical workspace-relative path.

    ``max_total_bytes`` defaults to ``DEFAULT_MAX_TOTAL_BYTES`` at call time
    (so tests can patch the module constant).
    """
    if max_total_bytes is None:
        max_total_bytes = DEFAULT_MAX_TOTAL_BYTES
    snapshot_dir = pathlib.Path(run_dir) / SNAPSHOT_DIR_NAME
    _mkdir_0700(snapshot_dir)
    entries: Dict[str, ProtectedPathEntry] = {}
    abs_paths: Dict[str, str] = {}
    total = 0
    root = pathlib.Path(repo_root)

    for rel, abs_path in sorted(
        iter_existing_protected_path_map(root).items(), key=lambda item: item[0]
    ):
        abs_paths[rel] = str(abs_path)
        try:
            lst = abs_path.lstat()
        except OSError as exc:
            _log("snapshot_protected_paths", "lstat failed for {}: {}".format(rel, exc))
            entries[rel] = ProtectedPathEntry(
                relative=rel, existed=True, snapshotted=False, size=0, reason="stat-failed"
            )
            continue
        mode = stat.S_IMODE(lst.st_mode)
        # A pre-existing symlink is snapshotted as metadata (target only): restore
        # recreates the link, instead of deleting it as if it never existed.
        if stat.S_ISLNK(lst.st_mode):
            try:
                target = os.readlink(str(abs_path))
            except OSError as exc:
                _log("snapshot_protected_paths", "readlink failed for {}: {}".format(rel, exc))
                entries[rel] = ProtectedPathEntry(
                    relative=rel, existed=True, snapshotted=False, size=0, reason="readlink-failed"
                )
                continue
            entries[rel] = ProtectedPathEntry(
                relative=rel, existed=True, snapshotted=True, size=0, reason=None,
                mode=mode, symlink_target=target,
            )
            continue
        size = lst.st_size
        if total + size > max_total_bytes:
            _log(
                "snapshot_protected_paths",
                "over-cap skip {}: size={} budget_left={}".format(
                    rel, size, max_total_bytes - total
                ),
            )
            entries[rel] = ProtectedPathEntry(
                relative=rel,
                existed=True,
                snapshotted=False,
                size=size,
                reason="over-cap",
            )
            continue
        dest = snapshot_dir / rel
        try:
            _mkdir_0700(dest.parent)
            shutil.copyfile(str(abs_path), str(dest))
            _chmod_0600(dest)
        except OSError as exc:
            _log("snapshot_protected_paths", "copy failed for {}: {}".format(rel, exc))
            entries[rel] = ProtectedPathEntry(
                relative=rel,
                existed=True,
                snapshotted=False,
                size=size,
                reason="copy-failed",
            )
            continue
        total += size
        entries[rel] = ProtectedPathEntry(
            relative=rel, existed=True, snapshotted=True, size=size, reason=None, mode=mode
        )

    manifest = {
        "maxTotalBytes": max_total_bytes,
        "totalBytes": total,
        "absPaths": dict(sorted(abs_paths.items())),
        "paths": {
            rel: {
                "existed": entry.existed,
                "snapshotted": entry.snapshotted,
                "size": entry.size,
                "reason": entry.reason,
                "mode": entry.mode,
                "symlinkTarget": entry.symlink_target,
            }
            for rel, entry in sorted(entries.items())
        },
    }
    manifest_path = snapshot_dir / MANIFEST_NAME
    try:
        fd = os.open(
            str(manifest_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        _log("snapshot_protected_paths", "manifest write failed: {}".format(exc))

    _log(
        "snapshot_protected_paths",
        "snapshotted {} path(s), {} byte(s)".format(
            sum(1 for e in entries.values() if e.snapshotted), total
        ),
    )
    return ProtectedSnapshot(
        run_dir=pathlib.Path(run_dir),
        snapshot_dir=snapshot_dir,
        entries=entries,
        total_bytes=total,
        max_total_bytes=max_total_bytes,
        abs_paths=abs_paths,
    )


def restore_protected_paths(
    repo_root: pathlib.Path,
    snapshot: ProtectedSnapshot,
    offenders: Sequence[str],
) -> RestoreResult:
    """Restore each offending protected path to its pre-run state.

    - existed + snapshotted -> overwrite with snapshot bytes at the **actual**
      absolute path (gitfile targets included; never under a gitfile path)
    - did not exist pre-run (absent from index) -> delete if present at the
      resolved absolute path
    - existed but unsnapshottable -> unrestored with honest over-cap message
    Restore failures are collected in ``errors`` and never swallowed.
    """
    root = pathlib.Path(repo_root)
    restored: List[str] = []
    unrestored: List[str] = []
    errors: List[Dict[str, str]] = []
    over_cap = False
    # Prefer snapshot abs map; fall back to live discovery for planted offenders.
    git_roots = None
    try:
        git_roots = discover_workspace_git_roots(root)
    except GrokWrapperError as exc:
        _log("restore_protected_paths", "git root rediscovery failed: {}".format(exc))

    def _abs_for(rel: str) -> pathlib.Path:
        stored = (snapshot.abs_paths or {}).get(rel)
        if stored:
            return pathlib.Path(stored)
        return resolve_protected_abs_path(root, rel, git_roots=git_roots)

    for raw in offenders:
        rel = _posix_rel(raw)
        if not rel:
            continue
        abs_path = _abs_for(rel)
        entry = snapshot.entries.get(rel)

        if entry is None:
            # Absent from pre-run index. Only auto-delete paths that would have
            # been snapshotted if they existed (Grok-created .env/.pem/hooks).
            # Other .git/* (e.g. index) are detect-only without a snapshot.
            if not is_snapshot_scope(rel):
                unrestored.append(rel)
                errors.append(
                    {
                        "path": rel,
                        "error": "protected path not in pre-run snapshot scope; restore it yourself",
                    }
                )
                continue
            try:
                if abs_path.exists() or abs_path.is_symlink():
                    if abs_path.is_dir() and not abs_path.is_symlink():
                        shutil.rmtree(str(abs_path))
                    else:
                        abs_path.unlink()
                restored.append(rel)
            except OSError as exc:
                _log("restore_protected_paths", "delete failed for {}: {}".format(rel, exc))
                unrestored.append(rel)
                errors.append({"path": rel, "error": str(exc)})
            continue

        if not entry.snapshotted:
            unrestored.append(rel)
            if entry.reason == "over-cap":
                over_cap = True
            else:
                errors.append(
                    {
                        "path": rel,
                        "error": "not snapshotted pre-run ({})".format(entry.reason or "unknown"),
                    }
                )
            continue

        # Pre-run symlink: recreate the exact link (no bytes were copied).
        if entry.symlink_target is not None:
            try:
                if abs_path.is_dir() and not abs_path.is_symlink():
                    shutil.rmtree(str(abs_path))
                elif abs_path.is_symlink() or abs_path.exists():
                    abs_path.unlink()
                parent = abs_path.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                os.symlink(entry.symlink_target, str(abs_path))
                restored.append(rel)
            except OSError as exc:
                _log("restore_protected_paths", "symlink restore failed for {}: {}".format(rel, exc))
                unrestored.append(rel)
                errors.append({"path": rel, "error": str(exc)})
            continue

        src = snapshot.snapshot_dir / rel
        try:
            if not src.is_file():
                raise OSError("snapshot file missing: {}".format(src))
            parent = abs_path.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            if abs_path.is_dir() and not abs_path.is_symlink():
                shutil.rmtree(str(abs_path))
            elif abs_path.is_symlink() or abs_path.exists():
                abs_path.unlink()
            shutil.copyfile(str(src), str(abs_path))
            # Reapply the pre-run permission bits: copyfile recreates the file at
            # the process umask, so a 0600 credential would come back world-readable
            # (review). Fall back to 0600 when the pre-run mode is unknown.
            try:
                os.chmod(str(abs_path), entry.mode if entry.mode else 0o600)
            except OSError as exc:
                _log("restore_protected_paths", "chmod restore failed for {}: {}".format(rel, exc))
            restored.append(rel)
        except OSError as exc:
            _log("restore_protected_paths", "restore failed for {}: {}".format(rel, exc))
            unrestored.append(rel)
            errors.append({"path": rel, "error": str(exc)})

    honest: Optional[str] = None
    if over_cap:
        honest = (
            "protected path changed and was too large to roll back; restore it yourself"
        )
    return RestoreResult(
        restored=restored,
        unrestored=unrestored,
        errors=errors,
        honest_message=honest,
    )


def raise_protected_path_write(
    offenders: Sequence[str],
    restore: RestoreResult,
) -> None:
    """Raise protected-path-write with restore detail (never claims false success)."""
    from groklib import GrokWrapperError

    ordered = sorted(_posix_rel(p) for p in offenders if _posix_rel(p))
    message = "Grok wrote to a protected path inside the repository: {}".format(
        ", ".join(ordered)
    )
    if restore.honest_message:
        message = message + "; " + restore.honest_message
    detail: Dict[str, object] = {
        "protectedPaths": ordered,
        "restored": list(restore.restored),
        "unrestored": list(restore.unrestored),
    }
    if restore.errors:
        detail["restoreErrors"] = list(restore.errors)
    if restore.honest_message:
        detail["rollbackNote"] = restore.honest_message
    _log(
        "raise_protected_path_write",
        "protected-path-write offenders={} restored={} unrestored={}".format(
            ordered, restore.restored, restore.unrestored
        ),
    )
    raise GrokWrapperError("protected-path-write", message, detail)
