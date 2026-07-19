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
#   - Snapshot ``git_roots`` baseline maps every logical prefix to the actual
#     gitdir; restore/plant-delete use that map even if a gitfile pointer is
#     rewritten after the run (live rediscovery must not override baseline).
#   - It does NOT protect against reads (documented D-SECRETREAD gap: Grok can
#     still read .env / keys inside the repo).
#   - Over-cap protected files / discovery overflow fail closed with honest
#     messages rather than claiming full coverage or restore.
#   - Backlog: probe seatbelt write-deny subpaths for true prevention.
#   - Git discovery lives in direct_protect_git.py (900-line cap split).

import dataclasses
import json
import os
import pathlib
import shutil
import stat
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib.modes import direct_protect_git as _git

# Re-export discovery API (single public surface via direct_protect).
MAX_GIT_TREE_WALK_FILES = _git.MAX_GIT_TREE_WALK_FILES
MAX_NESTED_GIT_DISCOVERY = _git.MAX_NESTED_GIT_DISCOVERY
is_sensitive_git_suffix = _git.is_sensitive_git_suffix
is_sensitive_git_relative = _git.is_sensitive_git_relative
is_snapshot_scope = _git.is_snapshot_scope
iter_git_tree_entries = _git.iter_git_tree_entries
discover_workspace_git_roots = _git.discover_workspace_git_roots
discover_workspace_gitfiles = _git.discover_workspace_gitfiles
iter_sensitive_git_entries = _git.iter_sensitive_git_entries
merge_git_root_pairs = _git.merge_git_root_pairs
resolve_protected_abs_path = _git.resolve_protected_abs_path
_normalize_git_roots = _git._normalize_git_roots
_git_rel_parts = _git._git_rel_parts

SNAPSHOT_DIR_NAME = "protected-snapshot"
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_TOTAL_BYTES = 25 * 1024 * 1024
# Gitfile marker bytes live under this store prefix so they never collide with
# sensitive children (``.git/HEAD`` needs a directory ``.git/`` in the store).
_SNAPSHOT_GITFILE_MARKER_PREFIX = "_gitfile_markers"


def _snapshot_store_rel(logical: str, abs_path: pathlib.Path) -> str:
    """Relative path under snapshot_dir for a logical protected key."""
    rel = _posix_rel(logical)
    try:
        is_marker_file = (
            abs_path.is_file()
            and not abs_path.is_symlink()
            and pathlib.Path(rel).name == ".git"
        )
    except OSError:
        is_marker_file = False
    if is_marker_file:
        return _SNAPSHOT_GITFILE_MARKER_PREFIX + "/" + rel
    return rel


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

    ``git_roots`` maps every discovered logical git prefix (``.git``,
    ``vendor/lib/.git``, ``.git/modules/sub``, ...) to the absolute gitdir
    present at snapshot time. Restore and plant-delete MUST use this baseline
    mapping even if a gitfile pointer is rewritten after the run; live
    rediscovery must not override a snapshotted prefix.
    """

    run_dir: pathlib.Path
    snapshot_dir: pathlib.Path
    entries: Dict[str, ProtectedPathEntry]
    total_bytes: int
    max_total_bytes: int
    abs_paths: Dict[str, str] = dataclasses.field(default_factory=dict)
    git_roots: Dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class RestoreResult:
    """Outcome of rolling back protected offenders."""

    restored: List[str]
    unrestored: List[str]
    errors: List[Dict[str, str]]
    honest_message: Optional[str] = None


def _posix_rel(path: str) -> str:
    return _git._posix_rel(path)


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
    return _git._is_snapshot_candidate(path)


def iter_existing_protected_paths(repo_root: pathlib.Path) -> Iterable[str]:
    """Yield logical repo-relative POSIX paths of existing protected files."""
    for rel, _abs in iter_existing_protected_path_map(repo_root).items():
        yield rel


def iter_existing_protected_path_map(
    repo_root: pathlib.Path,
) -> Dict[str, pathlib.Path]:
    """Map logical protected relative path -> actual absolute path to snapshot.

    Sensitive git metadata uses discovered gitdir abs paths (gitfile-safe).
    Gitfile *marker* files (``.git`` / ``vendor/lib/.git`` as a file) are
    snapshotted under their logical key to the marker path itself (not the
    target dir). Other deny-glob matches resolve under ``repo_root``.
    """
    # Late import: direct_finalize imports this module for restore.
    from groklib.modes.direct_finalize import path_matches_deny

    root = pathlib.Path(repo_root)
    mapping: Dict[str, pathlib.Path] = {}
    for rel, abs_path in iter_sensitive_git_entries(root):
        mapping[rel] = abs_path

    # Snapshot every in-workspace gitfile marker under its logical key.
    for pfx, gitfile in discover_workspace_gitfiles(root):
        if pfx not in mapping and _is_snapshot_candidate(gitfile):
            mapping[pfx] = gitfile

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
    # Baseline every logical prefix -> actual gitdir at snapshot time so restore
    # survives a post-run gitfile pointer rewrite (live rediscovery must not win).
    git_roots: Dict[str, str] = {}
    for prefix, abs_dir in discover_workspace_git_roots(root):
        pfx = _posix_rel(prefix).rstrip("/")
        if not pfx:
            continue
        try:
            git_roots[pfx] = str(pathlib.Path(abs_dir).resolve())
        except OSError:
            git_roots[pfx] = str(abs_dir)

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
        store_rel = _snapshot_store_rel(rel, abs_path)
        dest = snapshot_dir / store_rel
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
        "gitRoots": dict(sorted(git_roots.items())),
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
        git_roots=git_roots,
    )


def protected_paths_diverged_from_snapshot(
    repo_root: pathlib.Path,
    snapshot: ProtectedSnapshot,
) -> List[str]:
    """Logical protected keys whose on-disk state diverges from ``snapshot``.

    Fallback when a full tree re-diff is unavailable or untrusted (for example
    corrupted ``.git/HEAD``). Compares snapshotted content/mode/symlink targets
    and treats post-snapshot plants under the protected inventory as changed.
    Never silently reports clean for covered deny/sensitive paths.
    """
    root = pathlib.Path(repo_root)
    baseline_roots = dict(snapshot.git_roots or {})
    merged: Dict[str, pathlib.Path] = {
        pfx: pathlib.Path(abs_s) for pfx, abs_s in baseline_roots.items()
    }
    try:
        for pfx, abs_dir in discover_workspace_git_roots(root):
            key = _posix_rel(pfx).rstrip("/")
            if key and key not in merged:
                merged[key] = abs_dir
    except GrokWrapperError as exc:
        _log(
            "protected_paths_diverged_from_snapshot",
            "git root rediscovery failed: {}".format(exc),
        )

    def _abs_for(rel: str) -> pathlib.Path:
        return resolve_protected_abs_path(
            root,
            rel,
            git_roots=merged,
            abs_paths=snapshot.abs_paths or {},
        )

    def _on_disk_mode(path: pathlib.Path) -> Optional[int]:
        try:
            return stat.S_IMODE(path.lstat().st_mode)
        except OSError:
            return None

    diverged: Set[str] = set()

    for rel, entry in snapshot.entries.items():
        rel = _posix_rel(rel)
        if not rel:
            continue
        abs_path = _abs_for(rel)
        try:
            exists = abs_path.exists() or abs_path.is_symlink()
        except OSError:
            exists = False
        if not entry.existed:
            if exists:
                diverged.add(rel)
            continue
        if not exists:
            diverged.add(rel)
            continue
        if entry.symlink_target is not None:
            try:
                if not abs_path.is_symlink() or os.readlink(str(abs_path)) != entry.symlink_target:
                    diverged.add(rel)
            except OSError:
                diverged.add(rel)
            continue
        if not entry.snapshotted:
            # Unsnapshottable: compare size + mode; any mismatch is an offender so
            # restore can report unrestored instead of silent clean.
            try:
                lst = abs_path.lstat()
            except OSError:
                diverged.add(rel)
                continue
            if not stat.S_ISREG(lst.st_mode):
                diverged.add(rel)
                continue
            if lst.st_size != entry.size or (
                entry.mode and stat.S_IMODE(lst.st_mode) != entry.mode
            ):
                diverged.add(rel)
            continue
        src = snapshot.snapshot_dir / _snapshot_store_rel(rel, abs_path)
        if not src.is_file():
            legacy = snapshot.snapshot_dir / rel
            if legacy.is_file():
                src = legacy
        try:
            if not src.is_file():
                diverged.add(rel)
                continue
            if abs_path.is_symlink() or not abs_path.is_file():
                diverged.add(rel)
                continue
            if abs_path.read_bytes() != src.read_bytes():
                diverged.add(rel)
                continue
            mode = _on_disk_mode(abs_path)
            if entry.mode and mode is not None and mode != entry.mode:
                diverged.add(rel)
        except OSError:
            diverged.add(rel)

    # Plants created after the snapshot (not in the pre-run index).
    try:
        live_map = iter_existing_protected_path_map(root)
    except Exception as exc:
        _log(
            "protected_paths_diverged_from_snapshot",
            "live protected inventory failed: {}".format(exc),
        )
        live_map = {}
    for rel in live_map:
        key = _posix_rel(rel)
        if key and key not in snapshot.entries:
            diverged.add(key)

    return sorted(diverged)



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

    Absolute path resolution prefers (1) per-path ``abs_paths``, then (2) the
    snapshotted ``git_roots`` prefix map, then (3) live discovery only for
    prefixes absent from the baseline. A post-run gitfile pointer rewrite must
    not redirect plant-delete or byte restore away from the original common dir.
    """
    root = pathlib.Path(repo_root)
    restored: List[str] = []
    unrestored: List[str] = []
    errors: List[Dict[str, str]] = []
    over_cap = False
    # Baseline prefix map wins over live rediscovery (pointer-rewrite safety).
    baseline_roots = dict(snapshot.git_roots or {})
    if not baseline_roots and snapshot.abs_paths:
        # Legacy snapshots without git_roots: derive prefixes from abs_paths.
        for rel, abs_s in snapshot.abs_paths.items():
            under = _git_rel_parts(rel)
            if under is None:
                continue
            # logical prefix is everything through the .git segment
            parts = [p for p in _posix_rel(rel).split("/") if p]
            try:
                idx = parts.index(".git")
            except ValueError:
                continue
            pfx = "/".join(parts[: idx + 1])
            if pfx in baseline_roots:
                continue
            abs_path = pathlib.Path(abs_s)
            # strip sensitive suffix under gitdir
            suffix = "/".join(parts[idx + 1 :])
            git_dir = abs_path
            if suffix:
                # walk up by suffix depth
                for _ in suffix.split("/"):
                    git_dir = git_dir.parent
            baseline_roots[pfx] = str(git_dir)
    live_roots: List[Tuple[str, pathlib.Path]] = []
    try:
        live_roots = list(discover_workspace_git_roots(root))
    except GrokWrapperError as exc:
        _log("restore_protected_paths", "git root rediscovery failed: {}".format(exc))
    # Baseline-preferring single map for primary restore target.
    merged: Dict[str, pathlib.Path] = {
        pfx: pathlib.Path(abs_s) for pfx, abs_s in baseline_roots.items()
    }
    for pfx, abs_dir in live_roots:
        key = _posix_rel(pfx).rstrip("/")
        if key and key not in merged:
            merged[key] = abs_dir
    # Full union pairs (baseline + live) for multi-abs plant cleanup.
    union_pairs = merge_git_root_pairs(baseline_roots, live_roots)

    def _abs_for(rel: str) -> pathlib.Path:
        # Order: abs_paths exact -> baseline git_roots children -> live-only.
        # Bare gitfile marker keys never map to target dirs.
        return resolve_protected_abs_path(
            root,
            rel,
            git_roots=merged,
            abs_paths=snapshot.abs_paths or {},
        )

    def _all_abs_for(rel: str) -> List[pathlib.Path]:
        """Every abs path for logical rel across baseline+live gitdirs.

        Bare marker keys (``.git`` / ``vendor/lib/.git``) resolve only via
        abs_paths / workspace path - never every aliased target dir.
        """
        found: List[pathlib.Path] = []
        seen: Set[str] = set()

        def _remember(candidate: pathlib.Path) -> None:
            try:
                key = str(candidate.resolve())
            except OSError:
                key = str(candidate)
            if key in seen:
                return
            seen.add(key)
            found.append(candidate)

        _remember(_abs_for(rel))
        # Children under a logical prefix: baseline + live abs gitdirs.
        bare_marker = False
        for pfx, _git_dir in union_pairs:
            if _posix_rel(pfx).rstrip("/") == rel:
                bare_marker = True
                break
        if bare_marker:
            return found
        for pfx, git_dir in sorted(union_pairs, key=lambda item: len(item[0]), reverse=True):
            pfx_n = _posix_rel(pfx).rstrip("/")
            if not pfx_n:
                continue
            if rel.startswith(pfx_n + "/"):
                _remember(pathlib.Path(git_dir) / rel[len(pfx_n) + 1 :])
        return found

    def _delete_path(abs_path: pathlib.Path) -> Optional[str]:
        try:
            if abs_path.exists() or abs_path.is_symlink():
                if abs_path.is_dir() and not abs_path.is_symlink():
                    shutil.rmtree(str(abs_path))
                else:
                    abs_path.unlink()
            if abs_path.exists() or abs_path.is_symlink():
                return "protected plant still present after delete attempt"
            return None
        except OSError as exc:
            return str(exc)

    def _is_gitfile_marker_key(rel: str) -> bool:
        return rel == ".git" or rel.endswith("/.git")

    # Restore gitfile markers first so child residual checks see fixed pointers.
    ordered_offenders = sorted(
        (_posix_rel(raw) for raw in offenders if _posix_rel(raw)),
        key=lambda r: (0 if _is_gitfile_marker_key(r) else 1, r),
    )

    for rel in ordered_offenders:
        abs_path = _abs_for(rel)
        entry = snapshot.entries.get(rel)

        if entry is None:
            # Absent from pre-run index. Only auto-delete paths that would have
            # been snapshotted if they existed (Grok-created .env/.pem/hooks).
            # Other .git/* (e.g. index, bare gitfile pointer) are detect-only.
            # Use baseline+live git_roots so multi-component / reserved-name
            # modules/** plants classify by real prefix, not token heuristics.
            if not is_snapshot_scope(rel, git_roots=merged):
                unrestored.append(rel)
                errors.append(
                    {
                        "path": rel,
                        "error": "protected path not in pre-run snapshot scope; restore it yourself",
                    }
                )
                continue
            # Delete plant on every baseline+live abs for this logical key.
            remaining = False
            last_err: Optional[str] = None
            for candidate in _all_abs_for(rel):
                err = _delete_path(candidate)
                if err:
                    remaining = True
                    last_err = err
            if remaining:
                unrestored.append(rel)
                errors.append(
                    {
                        "path": rel,
                        "error": last_err or "protected plant still present after delete attempt",
                    }
                )
            else:
                restored.append(rel)
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

        src = snapshot.snapshot_dir / _snapshot_store_rel(rel, abs_path)
        if not src.is_file():
            # Legacy snapshots stored markers under the logical path.
            legacy = snapshot.snapshot_dir / rel
            if legacy.is_file():
                src = legacy
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
            # Snapshotted restores write only the baseline abs_paths target. Do not
            # delete live-only aliases for the same logical key (a redirected
            # gitfile target may hold independent content). Residual live-side
            # paths are reported unrestored only when the gitfile marker for
            # that prefix was not restored in this call (pointer still wrong).
            restored.append(rel)
            try:
                primary_key = str(abs_path.resolve())
            except OSError:
                primary_key = str(abs_path)
            for pfx, live_abs in live_roots:
                pfx_n = _posix_rel(pfx).rstrip("/")
                if not pfx_n or not (
                    rel == pfx_n or rel.startswith(pfx_n + "/")
                ):
                    continue
                base_s = baseline_roots.get(pfx_n)
                if base_s:
                    try:
                        if str(pathlib.Path(base_s).resolve()) == str(
                            pathlib.Path(live_abs).resolve()
                        ):
                            continue  # live still matches baseline
                    except OSError:
                        if str(base_s) == str(live_abs):
                            continue
                # Marker restored this call => pointer fixed; ignore stale pre-restore live map.
                if pfx_n in restored:
                    continue
                if rel == pfx_n:
                    continue  # marker itself handled by abs_paths
                if not rel.startswith(pfx_n + "/"):
                    continue
                candidate = pathlib.Path(live_abs) / rel[len(pfx_n) + 1 :]
                try:
                    ckey = str(candidate.resolve())
                except OSError:
                    ckey = str(candidate)
                if ckey == primary_key:
                    continue
                if candidate.exists() or candidate.is_symlink():
                    if rel not in unrestored:
                        unrestored.append(rel)
                    errors.append(
                        {
                            "path": rel,
                            "error": (
                                "live redirect path still present after baseline "
                                "restore; clear it yourself"
                            ),
                        }
                    )
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
