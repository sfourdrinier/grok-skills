# wrapper/scripts/groklib/modes/direct_protect.py
#
# Pre-run snapshot + post-run restore of protected paths for hardened-direct.
#
# SECURITY HONESTY:
#   - Direct mode does NOT prevent protected writes at the sandbox layer
#     (workspace profile is whole-root writable). This module rolls back the
#     COVERED protected set after a run (byte-identical if snapshotted; removed
#     if Grok created it): .env/keys plus .git config/HEAD/packed-refs/hooks and
#     .git/refs/** (a moved/created ref is reverted/removed). .git/index is
#     detect-only (git rebuilds it); loose .git/objects are not tracked (inert
#     until a watched ref points at them).
#   - It does NOT protect against reads (documented D-SECRETREAD gap: Grok can
#     still read .env / keys inside the repo).
#   - Over-cap protected files are recorded as unsnapshottable: fail closed with
#     an honest "too large to roll back" message rather than claiming restore.
#   - Backlog: probe seatbelt write-deny subpaths for true prevention.

import dataclasses
import json
import os
import pathlib
import shutil
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from groklib import log_stderr

SNAPSHOT_DIR_NAME = "protected-snapshot"
MANIFEST_NAME = "manifest.json"
# Bound total snapshot payload so a huge .pem cannot fill the run dir.
DEFAULT_MAX_TOTAL_BYTES = 25 * 1024 * 1024

# Explicit .git file names snapshotted at repo root of ``.git`` (never walk
# .git/objects: loose objects are content-addressed and inert until a ref points
# at them; ``.git/refs/**`` IS snapshotted below so a ref move can be rolled back).
_GIT_SNAPSHOT_FILES: Tuple[str, ...] = ("config", "HEAD", "packed-refs")


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


@dataclasses.dataclass(frozen=True)
class ProtectedSnapshot:
    """Pre-run protected-path index + on-disk byte copies under the run dir."""

    run_dir: pathlib.Path
    snapshot_dir: pathlib.Path
    entries: Dict[str, ProtectedPathEntry]
    total_bytes: int
    max_total_bytes: int


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


def is_snapshot_scope(relative: str) -> bool:
    """True when a protected path is in the pre-run snapshot set if it exists.

    ``.git/config``, ``.git/HEAD``, ``.git/packed-refs``, ``.git/hooks/*``, and
    ``.git/refs/**`` are snapshotted (so a created ref is auto-deleted and a moved
    ref is byte-restored). Other ``.git/*`` paths (index, objects, ...) may be
    *detected* by the git dir guard but are not auto-deleted on restore when
    absent from the index (they likely existed pre-run without a snapshot).
    """
    rel = _posix_rel(relative)
    if not rel:
        return False
    if (
        rel in (".git/config", ".git/HEAD", ".git/packed-refs")
        or rel.startswith(".git/hooks/")
        or rel.startswith(".git/refs/")
    ):
        return True
    if rel == ".git" or rel.startswith(".git/"):
        return False
    from groklib.modes.direct_finalize import path_matches_deny

    return path_matches_deny(rel)


def iter_existing_protected_paths(repo_root: pathlib.Path) -> Iterable[str]:
    """Yield repo-relative POSIX paths of existing regular protected files.

    Snapshots ``.git/config``, ``.git/HEAD``, and ``.git/hooks/*`` only - does
    not walk ``.git/objects``. Other deny-glob matches are found via a bounded
    walk that prunes ``.git``.
    """
    # Late import: direct_finalize imports this module for restore.
    from groklib.modes.direct_finalize import path_matches_deny

    root = pathlib.Path(repo_root)
    git_dir = root / ".git"
    for name in _GIT_SNAPSHOT_FILES:
        candidate = git_dir / name
        if _is_regular_file(candidate):
            yield ".git/" + name
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        try:
            for child in sorted(hooks.iterdir()):
                if _is_regular_file(child):
                    yield ".git/hooks/" + child.name
        except OSError as exc:
            _log("iter_existing_protected_paths", "hooks walk failed: {}".format(exc))
    refs = git_dir / "refs"
    if refs.is_dir():
        try:
            for dirpath, dirnames, filenames in os.walk(str(refs), followlinks=False):
                dirnames.sort()
                for fname in sorted(filenames):
                    child = pathlib.Path(dirpath) / fname
                    if _is_regular_file(child):
                        yield ".git/" + _posix_rel(os.path.relpath(str(child), str(git_dir)))
        except OSError as exc:
            _log("iter_existing_protected_paths", "refs walk failed: {}".format(exc))

    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        rel_dir = _posix_rel(os.path.relpath(dirpath, str(root)))
        if rel_dir == ".":
            rel_dir = ""
        # Never descend into .git (handled above for the sensitive subset).
        dirnames[:] = [d for d in dirnames if not (rel_dir == "" and d == ".git") and d != ".git"]
        for name in filenames:
            if rel_dir:
                rel = rel_dir + "/" + name
            else:
                rel = name
            rel = _posix_rel(rel)
            if not path_matches_deny(rel):
                continue
            candidate = root / rel
            if _is_regular_file(candidate):
                yield rel


def snapshot_protected_paths(
    repo_root: pathlib.Path,
    run_dir: pathlib.Path,
    *,
    max_total_bytes: Optional[int] = None,
) -> ProtectedSnapshot:
    """Copy pre-run protected file bytes under ``run_dir/protected-snapshot/``.

    Directory mode 0700; snapshot files 0600. Paths larger than the remaining
    budget are recorded as unsnapshottable (``snapshotted=False``, reason
    ``over-cap``) without copying.

    ``max_total_bytes`` defaults to ``DEFAULT_MAX_TOTAL_BYTES`` at call time
    (so tests can patch the module constant).
    """
    if max_total_bytes is None:
        max_total_bytes = DEFAULT_MAX_TOTAL_BYTES
    snapshot_dir = pathlib.Path(run_dir) / SNAPSHOT_DIR_NAME
    _mkdir_0700(snapshot_dir)
    entries: Dict[str, ProtectedPathEntry] = {}
    total = 0
    root = pathlib.Path(repo_root)

    for rel in iter_existing_protected_paths(root):
        abs_path = root / rel
        try:
            size = abs_path.stat().st_size
        except OSError as exc:
            _log("snapshot_protected_paths", "stat failed for {}: {}".format(rel, exc))
            entries[rel] = ProtectedPathEntry(
                relative=rel, existed=True, snapshotted=False, size=0, reason="stat-failed"
            )
            continue
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
            relative=rel, existed=True, snapshotted=True, size=size, reason=None
        )

    manifest = {
        "maxTotalBytes": max_total_bytes,
        "totalBytes": total,
        "paths": {
            rel: {
                "existed": entry.existed,
                "snapshotted": entry.snapshotted,
                "size": entry.size,
                "reason": entry.reason,
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
    )


def restore_protected_paths(
    repo_root: pathlib.Path,
    snapshot: ProtectedSnapshot,
    offenders: Sequence[str],
) -> RestoreResult:
    """Restore each offending protected path to its pre-run state.

    - existed + snapshotted -> overwrite with snapshot bytes
    - did not exist pre-run (absent from index) -> delete if present
    - existed but unsnapshottable -> unrestored with honest over-cap message
    Restore failures are collected in ``errors`` and never swallowed.
    """
    root = pathlib.Path(repo_root)
    restored: List[str] = []
    unrestored: List[str] = []
    errors: List[Dict[str, str]] = []
    over_cap = False

    for raw in offenders:
        rel = _posix_rel(raw)
        if not rel:
            continue
        abs_path = root / rel
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
