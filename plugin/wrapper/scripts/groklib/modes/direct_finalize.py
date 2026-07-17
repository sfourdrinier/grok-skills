# wrapper/scripts/groklib/modes/direct_finalize.py
#
# Post-Grok finalize for hardened-direct mode. Ordered policy layers replace the
# worktree sentinel / HEAD-equals-base / forensic-patch / handoff-manifest theater:
#
#   1. changed-set fingerprint diff (baseline vs after)
#   2. realpath-under-repo per changed path
#   3. deny-glob scan -> protected-path-write (+ ROLLBACK via direct_protect)
#   4. contract writeScopes scan -> write-scope-violation
#   5. D1(b) gate-script integrity + build gate (reuse code helpers)
#   6. contract requiredValidation (reuse code._run_recorded_command)
#   7. re-diff (build/validation may have written)
#   8. dirty-overlap -> dirty-path-conflict unless --force
#
# SECURITY: direct mode does NOT prevent protected writes at the sandbox layer
# (workspace is whole-root). The deny scan + direct_protect snapshot/restore
# guarantee protected paths are rolled back to pre-run state (byte-identical or
# removed-if-created). Reads of .env/keys are NOT blocked (D-SECRETREAD gap).
# Backlog: probe seatbelt write-deny subpaths for true prevention.

import fnmatch
import pathlib
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
    "*.pem",
    "*.key",
    "*.p12",
    ".git/hooks/**",
    ".githooks/**",
)


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


def _stat_sig(path: pathlib.Path) -> str:
    try:
        st = path.lstat()
    except OSError:
        return "absent"
    return "{}:{}:{}".format(st.st_size, st.st_mtime_ns, st.st_mode)


def capture_git_dir_guard(repo_root: pathlib.Path) -> FrozenSet[Tuple[str, str]]:
    """Fingerprint sensitive paths under ``.git`` (working-tree fingerprint is blind to them).

    Watches config/HEAD/index/packed-refs/COMMIT_EDITMSG and every file under
    ``.git/hooks/``. A post-run set-difference surfaces Grok writes that the
    sandbox cannot block (workspace profile is whole-root).
    """
    git_dir = pathlib.Path(repo_root) / ".git"
    pairs: Set[Tuple[str, str]] = set()
    for name in ("config", "HEAD", "index", "packed-refs", "COMMIT_EDITMSG"):
        rel = ".git/" + name
        pairs.add((rel, _stat_sig(git_dir / name)))
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        try:
            for child in hooks.iterdir():
                if child.is_file() or child.is_symlink():
                    rel = ".git/hooks/" + child.name
                    pairs.add((rel, _stat_sig(child)))
        except OSError as exc:
            _log("capture_git_dir_guard", "hooks walk failed: {}".format(exc))
    return frozenset(pairs)


def _changed_paths(
    baseline_fp: FrozenSet[Tuple[str, str]], after_fp: FrozenSet[Tuple[str, str]]
) -> Set[str]:
    """Symmetric path-set difference of fingerprint pairs (same pattern as _shared.py:423-425)."""
    added_or_changed = {relative for relative, _fingerprint in (after_fp - baseline_fp)}
    removed_or_changed = {relative for relative, _fingerprint in (baseline_fp - after_fp)}
    return added_or_changed | removed_or_changed


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

    _assert_realpath_under_repo(repo_root, changed)
    _assert_deny_globs(changed, repo_root=repo_root, protect_snapshot=protect_snapshot)
    _assert_git_dir_untouched(
        stage.baseline_git_fp, repo_root, protect_snapshot=protect_snapshot
    )
    _assert_write_scopes(changed, contract)

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
    _assert_realpath_under_repo(repo_root, changed)
    _assert_deny_globs(changed, repo_root=repo_root, protect_snapshot=protect_snapshot)
    _assert_git_dir_untouched(
        stage.baseline_git_fp, repo_root, protect_snapshot=protect_snapshot
    )
    _assert_write_scopes(changed, contract)

    overlap = sorted(changed & dirty_paths)
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


def capture_pristine_manifest(
    repo_root: pathlib.Path, target_relative: str
) -> Tuple[Optional[str], Optional[Dict[str, object]]]:
    """Read committed package.json name+scripts from HEAD (D1(b) baseline)."""
    return code_continue.read_committed_manifest_fields_from_ref(
        repo_root, "HEAD", target_relative
    )
