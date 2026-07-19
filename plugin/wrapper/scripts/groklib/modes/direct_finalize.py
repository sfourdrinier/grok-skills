# wrapper/scripts/groklib/modes/direct_finalize.py
#
# Post-Grok finalize for hardened-direct mode. Ordered policy layers replace the
# worktree sentinel / HEAD-equals-base / forensic-patch / handoff-manifest theater:
#
#   1. changed-set fingerprint diff (baseline vs after)
#   2. realpath-under-repo per changed path
#   3. deny-glob scan over FULL changed-set -> protected-path-write (+ ROLLBACK)
#   4. contract writeScopes scan on SOURCE changes only -> write-scope-violation
#   5. D1(b) gate-script integrity + build gate (reuse code helpers)
#   6. contract requiredValidation (reuse code._run_recorded_command)
#   7. re-diff (build/validation may have written)
#   8. dirty-overlap on SOURCE changes only -> dirty-path-conflict unless --force
#
# SOURCE vs FULL changed-set (7.1c): repo_change_fingerprint includes gitignored
# paths so deny still catches .env. Scope + dirty-overlap use source_changed =
# changed minus git_ignored_paths (batch check-ignore), so __pycache__/*.pyc and
# other ignored byproducts from build/validation never fail those checks.
#
# SECURITY: direct mode does NOT prevent protected writes at the sandbox layer
# (workspace is whole-root). The deny scan + git-dir guard + direct_protect
# snapshot/restore roll back the COVERED protected set to pre-run state
# (byte-identical or removed-if-created): .env/keys, .git config/HEAD/packed-refs,
# .git/hooks/**, .git/refs/**. .git/index and .git/COMMIT_EDITMSG are NOT guarded
# (benign working state git rewrites on ordinary reads like `git status`); loose
# .git/objects are not tracked (content-addressed, inert until a watched ref
# points at them). Reads of .env/keys are NOT blocked (D-SECRETREAD gap).
# Backlog: probe seatbelt write-deny subpaths for true prevention.

import fnmatch
import hashlib
import os
import pathlib
import stat
import subprocess
import types
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import worktree as worktree_mod
from groklib import worktree_escape
from groklib.implementation_contract import normalize_repo_relative, path_in_scopes
from groklib.modes import code as code_mode
from groklib.modes import code_continue
from groklib.modes import direct_protect
from groklib.modes._direct import DirectFinalizeStage

# Paths Grok must never write in direct mode (operator's real .git/.env/keys/hooks
# sit INSIDE the sandbox writable root). Matched via fnmatch on POSIX-normalized
# repo-relative paths, plus a first-component check for ".git".
DENY_WRITE_GLOBS: Tuple[str, ...] = (
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    ".env/**",
    "*.pem",
    "*.key",
    "*.p12",
    "*.p8",
    ".git/hooks/**",
    ".githooks/**",
    # Credential/secret files that carry tokens or private keys.
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    ".npmrc",
    ".envrc",
    "credentials.json",
)

# Cap hooks/refs fingerprint walks (SSOT: direct_protect.MAX_GIT_TREE_WALK_FILES)
# so a pathological tree cannot stall finalize; over-cap still detects
# add/remove via the count-bearing sentinel.
_MAX_GIT_TREE_FILES = direct_protect.MAX_GIT_TREE_WALK_FILES


def _log(function: str, message: str) -> None:
    log_stderr("modes.direct_finalize", function, message)


def _posix_rel(path: str) -> str:
    """POSIX-normalize a repo-relative path WITHOUT stripping a leading dotfile name.

    Do not use ``str.lstrip('./')``: that treats '.' as a character class and
    would turn ``.env`` into ``env`` and ``.git/config`` into ``git/config``.
    """
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def path_matches_deny(path: str) -> bool:
    """True when a repo-relative path matches DENY_WRITE_GLOBS (or is under .git)."""
    norm = _posix_rel(path)
    if not norm:
        return False
    parts = norm.split("/")
    if parts[0] == ".git":
        return True
    base = parts[-1]
    for pattern in DENY_WRITE_GLOBS:
        if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(base, pattern):
            return True
    return False


# Hash git files up to this size; above it, fall back to a stat signature (refs,
# HEAD, config, packed-refs are tiny; a pathological multi-MB hook is the only
# thing that would hit this).
_MAX_GIT_HASH_BYTES = 4 * 1024 * 1024


def _git_watch_sig(path: pathlib.Path) -> str:
    """Content-addressed signature for a watched git file (review: hash, not stat).

    A branch-ref move rewrites one 41-byte SHA line to another with the SAME
    size; stat-only signatures (size:mtime:mode) miss it if mtime is coalesced
    or restored. Hash the (small) content instead so the set-difference always
    flips. Symlinks record their target; non-regular / oversized files fall back
    to a stat signature.
    """
    try:
        st = path.lstat()
    except OSError:
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        try:
            return "symlink:" + os.readlink(str(path))
        except OSError:
            return "symlink:?"
    if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_GIT_HASH_BYTES:
        return "stat:{}:{}:{}".format(st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode))
    try:
        with open(str(path), "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return "sha256:{}:{}".format(digest, stat.S_IMODE(st.st_mode))
    except OSError:
        return "stat:{}:{}:{}".format(st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode))


def _resolve_git_dirs(repo_root: pathlib.Path) -> Tuple[pathlib.Path, pathlib.Path]:
    """Resolve (per-worktree gitdir, common gitdir) via git; fall back to <root>/.git.

    In a LINKED git worktree ``.git`` is a FILE pointing at
    ``<common>/.git/worktrees/<name>``, and refs/config/packed-refs live in the
    COMMON dir - so a plain ``<root>/.git`` directory assumption is blind to a
    ref move there (review). HEAD is per-worktree; refs/config/packed-refs/hooks
    are common.
    """
    root = pathlib.Path(repo_root)

    def _rev_parse(flag: str) -> Optional[pathlib.Path]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), "rev-parse", flag],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        val = (completed.stdout or "").strip()
        if not val:
            return None
        p = pathlib.Path(val)
        return p if p.is_absolute() else (root / p).resolve()

    git_dir = _rev_parse("--absolute-git-dir") or (root / ".git")
    common_dir = _rev_parse("--git-common-dir") or git_dir
    return git_dir, common_dir


def capture_git_dir_guard(repo_root: pathlib.Path) -> FrozenSet[Tuple[str, str]]:
    """Fingerprint security-relevant git paths (working-tree fingerprint is blind to them).

    Watches config/packed-refs/hooks/refs in the COMMON git dir and HEAD in the
    per-worktree git dir (resolved via git, so a linked worktree whose ``.git``
    is a file is handled), each by CONTENT HASH so a same-size ref move is
    detected. Hooks and refs use the same bounded recursive inventory as
    protected snapshot (``direct_protect.iter_git_tree_entries``). A post-run
    set-difference surfaces Grok writes the sandbox cannot block (workspace
    profile is whole-root).

    Deliberately NOT watched (benign working state git rewrites on ordinary reads
    like ``git status``, which would otherwise false-positive every real run):
    ``.git/index`` and ``.git/COMMIT_EDITMSG``. Loose ``.git/objects`` are not
    fingerprinted: content-addressed and inert until a watched ref points at them.
    """
    git_dir, common_dir = _resolve_git_dirs(repo_root)
    pairs: Set[Tuple[str, str]] = set()
    # HEAD is per-worktree; config/packed-refs are shared (common dir).
    pairs.add((".git/HEAD", _git_watch_sig(git_dir / "HEAD")))
    for name in ("config", "packed-refs"):
        pairs.add((".git/" + name, _git_watch_sig(common_dir / name)))
    pairs |= _fingerprint_git_tree(common_dir, "hooks")
    pairs |= _fingerprint_git_tree(common_dir, "refs")
    return frozenset(pairs)


def _fingerprint_git_tree(git_dir: pathlib.Path, tree_name: str) -> Set[Tuple[str, str]]:
    """Fingerprint ``.git/<tree_name>/**`` via the shared protect inventory.

    Over-cap emits a single count-bearing sentinel so an add/remove past the
    cap still flips the set-difference, rather than silently going undetected.
    """
    pairs: Set[Tuple[str, str]] = set()
    count = 0
    for rel, abs_path in direct_protect.iter_git_tree_entries(
        git_dir, tree_name, max_files=_MAX_GIT_TREE_FILES
    ):
        pairs.add((rel, _git_watch_sig(abs_path)))
        count += 1
    if count >= _MAX_GIT_TREE_FILES:
        pairs.add((".git/{}/**".format(tree_name), "over-cap:{}".format(count)))
    return pairs


def _changed_paths(
    baseline_fp: FrozenSet[Tuple[str, str]], after_fp: FrozenSet[Tuple[str, str]]
) -> Set[str]:
    """Symmetric path-set difference of fingerprint pairs (same pattern as _shared.py:423-425)."""
    added_or_changed = {relative for relative, _fingerprint in (after_fp - baseline_fp)}
    removed_or_changed = {relative for relative, _fingerprint in (baseline_fp - after_fp)}
    return added_or_changed | removed_or_changed


def git_ignored_paths(repo_root: pathlib.Path, paths: Set[str]) -> Set[str]:
    """Return the subset of ``paths`` that git considers ignored under ``repo_root``.

    Batch ``git check-ignore --stdin -z`` over the candidate set (NUL-separated
    path stream). Empty ``paths`` or none-ignored (git exit 1) returns an empty
    set. Non-probe failures fail closed as worktree-failure. Used to derive
    source_changed for scope + dirty-overlap while the deny scan keeps the full
    changed-set.
    """
    if not paths:
        return set()
    # NUL-separated stdin matches git check-ignore --stdin -z contract.
    payload = "\0".join(sorted(paths)) + "\0"
    argv = [
        "git",
        "-c",
        "core.hooksPath={}".format(worktree_mod._EMPTY_GIT_HOOKS),
        "-C",
        str(repo_root),
        "check-ignore",
        "--stdin",
        "-z",
    ]
    try:
        completed = subprocess.run(
            argv,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            env=worktree_mod._git_env(),
            timeout=worktree_mod._GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log("git_ignored_paths", "check-ignore could not be executed: {}".format(exc))
        raise GrokWrapperError(
            "worktree-failure",
            "git check-ignore could not be executed: {}".format(exc),
            {"argv": argv},
        ) from exc
    # git check-ignore: 0 = one or more ignored, 1 = none ignored, 128 = fatal.
    if completed.returncode == 1:
        return set()
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        _log(
            "git_ignored_paths",
            "check-ignore failed exit={}: {}".format(completed.returncode, stderr),
        )
        raise GrokWrapperError(
            "worktree-failure",
            "git check-ignore failed while classifying changed paths",
            {"exitStatus": completed.returncode, "stderr": stderr},
        )
    return {part for part in completed.stdout.split("\0") if part}


def _source_changed_paths(repo_root: pathlib.Path, changed: Set[str]) -> Set[str]:
    """Changed paths minus git-ignored byproducts (for scope + dirty-overlap only)."""
    return changed - git_ignored_paths(repo_root, changed)


def _assert_realpath_under_repo(repo_root: pathlib.Path, changed: Set[str]) -> None:
    """Fail closed when any changed path realpath-escapes the repository root."""
    escaped: List[str] = []
    for relative in sorted(changed):
        candidate = (repo_root / relative).resolve()
        if not worktree_mod._is_within(candidate, repo_root):
            escaped.append(relative)
    if escaped:
        _log("_assert_realpath_under_repo", "paths escape repo root: {}".format(escaped))
        raise GrokWrapperError(
            "sandbox-failure",
            "changed path resolves outside the repository root",
            {"escapedPaths": escaped, "repository": str(repo_root)},
        )


def _rollback_and_raise_protected(
    offenders: List[str],
    *,
    repo_root: pathlib.Path,
    protect_snapshot: Optional["direct_protect.ProtectedSnapshot"],
) -> None:
    """Restore protected offenders from the pre-run snapshot, then raise."""
    if protect_snapshot is None:
        # Fail closed without claiming restore when snapshot was never taken.
        _log("_rollback_and_raise_protected", "no snapshot; cannot restore: {}".format(offenders))
        raise GrokWrapperError(
            "protected-path-write",
            "Grok wrote to a protected path inside the repository: {}".format(
                ", ".join(offenders)
            ),
            {
                "protectedPaths": offenders,
                "restored": [],
                "unrestored": list(offenders),
                "restoreErrors": [
                    {"path": p, "error": "no pre-run protected snapshot available"}
                    for p in offenders
                ],
            },
        )
    restore = direct_protect.restore_protected_paths(repo_root, protect_snapshot, offenders)
    direct_protect.raise_protected_path_write(offenders, restore)


def _assert_deny_globs(
    changed: Set[str],
    *,
    repo_root: pathlib.Path,
    protect_snapshot: Optional["direct_protect.ProtectedSnapshot"],
) -> None:
    """Fail closed as protected-path-write; restore protected paths before raising."""
    offenders = sorted(p for p in changed if path_matches_deny(p))
    if not offenders:
        return
    _log("_assert_deny_globs", "protected-path-write: {}".format(offenders))
    _rollback_and_raise_protected(
        offenders, repo_root=repo_root, protect_snapshot=protect_snapshot
    )


def _assert_git_dir_untouched(
    baseline_git_fp: FrozenSet[Tuple[str, str]],
    repo_root: pathlib.Path,
    *,
    protect_snapshot: Optional["direct_protect.ProtectedSnapshot"],
) -> None:
    """Fail closed when any watched ``.git/*`` path changed; restore then raise."""
    after = capture_git_dir_guard(repo_root)
    git_changed = sorted(_changed_paths(baseline_git_fp, after))
    if not git_changed:
        return
    _log("_assert_git_dir_untouched", "protected-path-write under .git: {}".format(git_changed))
    _rollback_and_raise_protected(
        git_changed, repo_root=repo_root, protect_snapshot=protect_snapshot
    )


def _assert_write_scopes(changed: Set[str], contract: Optional[dict]) -> None:
    """Fail closed as write-scope-violation when a changed path is outside writeScopes."""
    if not contract:
        return
    scopes = list(contract.get("writeScopes") or [])
    if not scopes:
        return
    for relative in sorted(changed):
        if not path_in_scopes(relative, scopes, from_git=True):
            _log("_assert_write_scopes", "out of scope: {}".format(relative))
            raise GrokWrapperError(
                "write-scope-violation",
                "changed path outside writeScopes: {}".format(relative),
                {"path": relative},
            )


def _run_required_validation(
    stage: DirectFinalizeStage,
    contract: Optional[dict],
    run_recorded_command,
) -> None:
    """Run contract requiredValidation commands under the real repo root."""
    if not contract or not contract.get("requiredValidation"):
        return
    repo_root = stage.repo_root
    for entry in contract["requiredValidation"]:
        argv = list(entry["argv"])
        rel_cwd = entry.get("cwd") or "."
        if rel_cwd in (".", "./", ""):
            cwd = repo_root
        else:
            try:
                rel = normalize_repo_relative(rel_cwd)
            except GrokWrapperError as exc:
                raise GrokWrapperError(
                    "validation-failure",
                    "invalid validation cwd",
                    {"error": str(exc)},
                ) from exc
            cwd = (repo_root / rel).resolve()
            if not worktree_mod._is_within(cwd, repo_root):
                raise GrokWrapperError(
                    "validation-failure",
                    "validation cwd escapes repository",
                    {"cwd": str(cwd)},
                )
        purpose = entry.get("purpose") or "contract-validation"
        record = run_recorded_command(argv, cwd, purpose)
        stage.acc.commands.append(record)
        if record["exitStatus"] != 0:
            raise GrokWrapperError(
                "validation-failure",
                "requiredValidation command failed (exit {})".format(record["exitStatus"]),
                {"purpose": purpose, "exitStatus": record["exitStatus"], "argv": argv},
            )


def _run_build_gate_for_direct(
    stage: DirectFinalizeStage,
    *,
    target_relative: str,
    package_manager: Optional[str],
    pm_binary: Optional[str],
    never_build_workspaces: Dict[str, Tuple[str, ...]],
    original_workspace_name: Optional[str],
    pristine_scripts: Optional[Dict[str, object]],
) -> None:
    """Reuse code._run_build_gate with a path-only worktree stand-in (cwd = repo root)."""
    path_only = types.SimpleNamespace(path=stage.repo_root)
    gate_stage = types.SimpleNamespace(
        worktree=path_only,
        acc=stage.acc,
        progress=stage.progress,
    )
    code_mode._run_build_gate(
        gate_stage,
        target_relative,
        package_manager,
        pm_binary,
        never_build_workspaces,
        original_workspace_name,
        pristine_scripts,
    )


def finalize_direct(
    stage: DirectFinalizeStage,
    *,
    contract: Optional[dict],
    target_relative: str,
    package_manager: Optional[str],
    pm_binary: Optional[str],
    never_build_workspaces: Dict[str, Tuple[str, ...]],
    original_workspace_name: Optional[str],
    pristine_scripts: Optional[Dict[str, object]],
) -> None:
    """Ordered direct finalize. Raises classified GrokWrapperError on policy failure."""
    repo_root = stage.repo_root
    baseline_fp = stage.baseline_fp
    dirty_paths = set(stage.dirty_paths)

    after_fp = worktree_escape.repo_change_fingerprint(repo_root)
    changed = _changed_paths(baseline_fp, after_fp)
    protect_snapshot = getattr(stage, "protect_snapshot", None)

    # Protected rollback runs BEFORE the realpath-escape check: a protected path
    # (.env / *.pem) replaced with a symlink that resolves outside the repo must
    # be restored from snapshot first, else _assert_realpath_under_repo raises
    # sandbox-failure and skips the rollback (review 5). Deny keeps the FULL
    # changed-set (incl. gitignored .env); scope uses source only.
    _assert_deny_globs(changed, repo_root=repo_root, protect_snapshot=protect_snapshot)
    _assert_git_dir_untouched(
        stage.baseline_git_fp, repo_root, protect_snapshot=protect_snapshot
    )
    _assert_realpath_under_repo(repo_root, changed)
    source_changed = _source_changed_paths(repo_root, changed)
    _assert_write_scopes(source_changed, contract)

    # D1(b): pristine scripts from HEAD (already captured at prepare); refuse
    # Grok-modified gate scripts, then run the build gate in the real tree.
    stage.progress.safe_emit("validate", "direct: running build gate")
    _run_build_gate_for_direct(
        stage,
        target_relative=target_relative,
        package_manager=package_manager,
        pm_binary=pm_binary,
        never_build_workspaces=never_build_workspaces,
        original_workspace_name=original_workspace_name,
        pristine_scripts=pristine_scripts,
    )

    stage.progress.safe_emit("validate", "direct: running requiredValidation")
    _run_required_validation(stage, contract, code_mode._run_recorded_command)

    # Re-diff: build/validation may have written further paths.
    after_fp = worktree_escape.repo_change_fingerprint(repo_root)
    changed = _changed_paths(baseline_fp, after_fp)
    _assert_deny_globs(changed, repo_root=repo_root, protect_snapshot=protect_snapshot)
    _assert_git_dir_untouched(
        stage.baseline_git_fp, repo_root, protect_snapshot=protect_snapshot
    )
    _assert_realpath_under_repo(repo_root, changed)
    source_changed = _source_changed_paths(repo_root, changed)
    _assert_write_scopes(source_changed, contract)

    # Dirty-overlap ignores gitignored byproducts (same source filter as scope).
    overlap = sorted(source_changed & dirty_paths)
    if overlap and not stage.force:
        _log("finalize_direct", "dirty-path-conflict: {}".format(overlap))
        raise GrokWrapperError(
            "dirty-path-conflict",
            "Grok modified path(s) that were already dirty in the operator checkout; "
            "re-run with --force to allow",
            {"overlappingPaths": overlap, "hint": "re-run with --force"},
        )

    stage.acc.changed_files = sorted(changed)
    stage.acc.effective_working_directory = str(repo_root)
    try:
        text = worktree_mod._git(repo_root, "diff", "--stat", "HEAD")
        stage.acc.diff_summary = text or None
    except Exception as exc:
        _log("finalize_direct", "diff summary unavailable: {}".format(exc))
        stage.acc.diff_summary = None

    stage.progress.safe_emit(
        "validate",
        "direct finalize complete",
        data={"changedFiles": list(stage.acc.changed_files)},
    )


def restore_protected_on_abort(
    repo_root: pathlib.Path,
    baseline_fp: FrozenSet[Tuple[str, str]],
    baseline_git_fp: FrozenSet[Tuple[str, str]],
    protect_snapshot: Optional["direct_protect.ProtectedSnapshot"],
) -> Dict[str, List[str]]:
    """Best-effort protected-path rollback for ABNORMAL direct exits (reviews 2/3/5).

    ``finalize_direct``'s ordered rollback only runs on the normal path. When
    Grok times out / cancels / exits nonzero (finalize never runs), or a
    build-gate / requiredValidation command writes a protected path then fails,
    the credential/.git writes would be left live. This re-diffs the tree + the
    .git guard and restores every protected offender from the pre-run snapshot.
    Idempotent: after a protected-path-write already restored, the re-diff finds
    nothing to restore. Never raises (returns a summary; logs failures).
    """
    offenders: Set[str] = set()
    try:
        after_fp = worktree_escape.repo_change_fingerprint(repo_root)
        offenders |= {p for p in _changed_paths(baseline_fp, after_fp) if path_matches_deny(p)}
    except Exception as exc:
        _log("restore_protected_on_abort", "changed-set re-diff failed: {}".format(exc))
    try:
        after_git = capture_git_dir_guard(repo_root)
        offenders |= set(_changed_paths(baseline_git_fp, after_git))
    except Exception as exc:
        _log("restore_protected_on_abort", "git-dir guard re-scan failed: {}".format(exc))

    ordered = sorted(offenders)
    if not ordered:
        return {"restored": [], "unrestored": [], "errors": []}
    if protect_snapshot is None:
        _log("restore_protected_on_abort", "no snapshot; cannot restore {}".format(ordered))
        return {"restored": [], "unrestored": ordered, "errors": []}
    restore = direct_protect.restore_protected_paths(repo_root, protect_snapshot, ordered)
    _log(
        "restore_protected_on_abort",
        "abnormal-exit rollback restored={} unrestored={}".format(
            restore.restored, restore.unrestored
        ),
    )
    return {
        "restored": list(restore.restored),
        "unrestored": list(restore.unrestored),
        "errors": [e.get("path", "") for e in restore.errors],
    }


def capture_pristine_manifest(
    repo_root: pathlib.Path, target_relative: str
) -> Tuple[Optional[str], Optional[Dict[str, object]]]:
    """Read committed package.json name+scripts from HEAD (D1(b) baseline)."""
    return code_continue.read_committed_manifest_fields_from_ref(
        repo_root, "HEAD", target_relative
    )
