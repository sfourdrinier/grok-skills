# wrapper/scripts/groklib/worktree_escape.py
#
# Escape-detection + change-fingerprinting subsystem for the code/verify worktree
# lifecycle, extracted from worktree.py (900-line cap). A linked git worktree is
# isolated from the main checkout, so worktree-side git status never reports writes
# that escaped into the operator's REAL checkout; these guards therefore scan the
# original checkout directly -- tracked divergence, untracked non-ignored files, and
# the gitignored set (Grok r5 #4) -- tolerating the operator's OWN pre-existing dirt
# via ``capture_original_checkout_baseline`` (a per-path entry fingerprint), and
# re-scanning the real checkout after the build gate (``assert_original_checkout_unmodified``).
#
# The subsystem reuses worktree.py's low-level git plumbing (worktree._git /
# worktree._git_query / worktree._within_any / worktree.diff_summary) through the
# ``worktree`` module, so the dependency is strictly one-directional
# (worktree_escape -> worktree) and never forms an import cycle.

import os
import pathlib
import stat
from typing import Dict, FrozenSet, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import path_inventory
from groklib import worktree
from groklib.worktree import ExternalWorktree


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "worktree_escape" component prefix."""
    log_stderr("worktree_escape", function, message)


# Build/test/cache artifact directory names a verification/build run may write into
# at ANY depth (per-package node_modules/dist/.next/... nested below the worktree
# root, not only at the root). A same-named path component alone does NOT tolerate a
# change -- a source-tree escape like ``packages/foo/src/dist/backdoor.ts`` also has a
# "dist" component. Tolerance is granted ONLY when git actually ignores the path in the
# worktree (see ``_is_ignored_artifact``); this name set is the cheap pre-filter.
ARTIFACT_DIR_NAMES: FrozenSet[str] = frozenset(
    {"node_modules", "dist", ".next", ".turbo", "coverage", "build", ".cache"}
)


def _has_artifact_component(relative: str) -> bool:
    """True when any path component of ``relative`` is a known build-artifact dir name."""
    return any(part in ARTIFACT_DIR_NAMES for part in pathlib.PurePath(relative).parts)


def _is_ignored_artifact(wt: "ExternalWorktree", relative: str) -> bool:
    """True only when ``relative`` is a build-artifact-named path that git IGNORES in ``wt``.

    A same-named component (``dist``/``build``/...) is never enough alone, or a
    source-tree escape like ``packages/foo/src/dist/backdoor.ts`` would slip the gate. The
    path must ALSO be genuinely gitignored inside the worktree (proven with ``git
    check-ignore``). Applied ONLY to the within-worktree changed-file gate, NEVER to the
    original-checkout escape-detection loop.
    """
    if not _has_artifact_component(relative):
        return False
    completed = worktree._git_query(wt.path, "check-ignore", "-q", "--", relative)
    # git check-ignore exits 0 when the path is ignored, 1 when it is not, and
    # 128 on error -- only a definitive "ignored" (0) grants tolerance.
    return completed.returncode == 0


def repo_change_fingerprint(repo_root: pathlib.Path) -> FrozenSet[Tuple[str, str]]:
    """Snapshot every changed repo-relative path AND a fingerprint of each (content+mode).

    Defense-in-depth for read-only modes (review): captured before and after the run, the
    two-way SET DIFFERENCE reveals any file the run wrote to (or reverted in) the real
    checkout even though its sandbox denies writes and it reported no edits. Covers tracked
    modifications, untracked non-ignored files, AND the gitignored set (Grok r5 #4). Each
    entry is ``(path, fingerprint)`` (git hash-object + mode for tracked/untracked; a
    bounded stat signature for ignored), NOT just the path, so a REWRITE or CHMOD of an
    already-dirty file is a NEW pair. A deleted/unreadable path signs as ``absent``.
    """
    resolved_repo_root = pathlib.Path(repo_root).resolve()
    # NUL-safe inventory (path_inventory): non -z listers C-quote non-ASCII under
    # default core.quotePath, producing phantom keys that break fingerprint rewrite
    # detection and dirty-path-conflict overlap.
    changed = set(path_inventory.list_working_tree_changed_paths(resolved_repo_root, "HEAD"))
    pairs = {
        (relative, _working_tree_fingerprint(resolved_repo_root, relative)) for relative in changed
    }
    # Grok r5 #4: the scan above is blind to gitignored paths, so add the ignored set
    # with a bounded stat signature -- a planted/rewritten/chmod'd ignored file is then
    # a NEW (path, signature) pair the before/after set-difference surfaces.
    for relative in path_inventory.list_ignored_untracked_paths(resolved_repo_root):
        pairs.add((relative, _ignored_path_signature(resolved_repo_root, relative)))
    return frozenset(pairs)


_ABSENT_FINGERPRINT = "absent"


def _on_disk_mode_token(repo_root: pathlib.Path, relative: str) -> str:
    """Return ``repo_root/relative``'s on-disk permission bits as a 4-digit octal string.

    Round7 mode-only-change-baseline-exemption-bypass: git hash-object is content-only and
    normalizes tracked modes, so a mode-only chmod on an already-dirty file evaded the
    rewrite gate. The real permission bits (via ``os.lstat``, never following a symlink)
    make any mode change a fingerprint change; an unstattable path signs as ``absent``.
    """
    try:
        file_stat = os.lstat(str(pathlib.Path(repo_root) / relative))
    except OSError:
        return _ABSENT_FINGERPRINT
    return format(stat.S_IMODE(file_stat.st_mode), "04o")


def _working_tree_fingerprint(repo_root: pathlib.Path, relative: str) -> str:
    """Return a content-AND-mode fingerprint of ``repo_root/relative``'s working-tree state.

    ``git hash-object`` (the on-disk blob hash) detects a content change regardless of
    index/HEAD state, joined with the on-disk permission bits so a mode-only mutation is
    caught too (Round7). An unhashable path signs its content as the fixed ``absent``.
    """
    completed = worktree._git_query(repo_root, "hash-object", "--", relative)
    content = _ABSENT_FINGERPRINT
    if completed.returncode == 0:
        digest = completed.stdout.strip()
        if digest:
            content = digest
    return "{}:{}".format(content, _on_disk_mode_token(repo_root, relative))


def _ignored_path_signature(repo_root: pathlib.Path, relative: str) -> str:
    """Return a bounded stat-only signature (size:mtime_ns:mode) of an IGNORED path.

    The gitignored set (node_modules, build caches) can be enormous, so content-hashing
    every entry is infeasible. A stat-only signature (one ``os.lstat``, no content read)
    detects a planted file, an in-place rewrite (size/mtime), and a chmod (mode), keeping
    the scan bounded (Grok r5 #4). An unstattable path signs as ``absent``.
    """
    try:
        file_stat = os.lstat(str(pathlib.Path(repo_root) / relative))
    except OSError:
        return _ABSENT_FINGERPRINT
    return "{}:{}:{}".format(
        file_stat.st_size, file_stat.st_mtime_ns, format(stat.S_IMODE(file_stat.st_mode), "04o")
    )


def capture_original_checkout_baseline(repo_root: pathlib.Path) -> Dict[str, str]:
    """Snapshot the ORIGINAL checkout's tracked divergence, untracked dirt, AND ignored set at entry.

    Maps every path already dirty in ``repo_root`` at run START -- tracked divergence,
    untracked non-ignored files, and the gitignored set (Grok r5 #4) -- to a fingerprint
    of its entry state. ``assert_changes_within`` exempts ONLY paths whose fingerprint is
    UNCHANGED since entry (the operator's own pre-existing work, explicitly supported),
    flagging a run-attributable rewrite/chmod (Round5 / Round7) or a planted file (absent
    from the baseline). Defense-in-depth (the real escape, a terminal absolute-path write,
    is already denied by sandbox write-confinement); never false-positives on pre-existing
    dirt.
    """
    resolved_repo_root = pathlib.Path(repo_root).resolve()
    baseline: Dict[str, str] = {
        relative: _working_tree_fingerprint(resolved_repo_root, relative)
        for relative in path_inventory.list_working_tree_changed_paths(
            resolved_repo_root, "HEAD"
        )
    }
    # Grok r5 #4: the original checkout must be UNTOUCHED, so its gitignored set is
    # baselined too (bounded stat signature) -- a pre-existing ignored file is exempt
    # while its signature is unchanged, one the run plants/rewrites/chmods is flagged.
    # Disjoint from the non-ignored set above, so no entry is overwritten.
    for relative in path_inventory.list_ignored_untracked_paths(resolved_repo_root):
        baseline[relative] = _ignored_path_signature(resolved_repo_root, relative)
    return baseline


def assert_changes_within(
    wt: ExternalWorktree,
    allowed_roots: Tuple[pathlib.Path, ...],
    worktree_changed: Optional[List[str]] = None,
    original_baseline: Optional[Dict[str, str]] = None,
) -> None:
    """Raise unexpected-edits if any change (worktree or original checkout) escapes ``allowed_roots``.

    Four surfaces are checked: (1) changed files in the worktree (under wt.path);
    (2) tracked divergence + non-ignored untracked files in the ORIGINAL checkout;
    (3) the ORIGINAL checkout's gitignored set (Grok r5 #4); and (4) baseline paths
    that VANISHED from the after-scans (a run-attributable delete/revert of the
    operator's pre-existing dirt, PR968 codex #3). See the module header for why the
    original checkout is scanned separately. When ``worktree_changed`` is supplied
    (verify's snapshot-scoped delta) it is used verbatim; otherwise the change set is
    the full diff_summary (code mode). ``original_baseline`` (from
    ``capture_original_checkout_baseline`` at run entry) fingerprints each
    already-dirty original-checkout path; it is exempt ONLY while its fingerprint is
    unchanged AND still present, so a run-attributable rewrite/chmod/plant/reversal is
    never silently blessed. Every offending absolute path is reported together.
    """
    resolved_roots = [pathlib.Path(root).resolve() for root in allowed_roots]
    baseline: Dict[str, str] = original_baseline if original_baseline is not None else {}
    violations: List[str] = []

    if worktree_changed is None:
        worktree_changed, _ = worktree.diff_summary(wt)
    wt_root = wt.path.resolve()
    for relative in worktree_changed:
        # Resolve first so a symlink under node_modules/ → /tmp cannot hide an
        # out-of-worktree write behind an "ignored artifact" name.
        candidate = (wt.path / relative).resolve()
        if not worktree._within_any(candidate, [wt_root]):
            violations.append(str(candidate))
            continue
        if _is_ignored_artifact(wt, relative):
            # In-worktree gitignored build artifact: tolerate (not a source edit).
            continue
        if not worktree._within_any(candidate, resolved_roots):
            violations.append(str(candidate))
            continue

    violations.extend(_collect_original_checkout_escapes(wt, resolved_roots, baseline))

    _raise_escape_violations(
        "assert_changes_within",
        "changes were written outside the allowed roots: {}",
        violations,
        resolved_roots,
    )


def _collect_original_checkout_escapes(
    wt: ExternalWorktree,
    resolved_roots: List[pathlib.Path],
    baseline: Dict[str, str],
) -> List[str]:
    """Collect every ORIGINAL-checkout escape (tracked/untracked/ignored/vanished) outside ``resolved_roots``.

    Extracted so the entry escape scan (``assert_changes_within``) and the
    post-build-gate re-scan (``assert_original_checkout_unmodified``) share ONE
    implementation of the real-checkout comparison (DRY). Escape detection in the
    operator's REAL checkout is defense-in-depth and is NEVER softened by artifact
    tolerance: any newly diverged path is a violation regardless of name. A path
    already dirty at entry is exempt ONLY while its fingerprint (content+mode for
    tracked/untracked, bounded stat signature for ignored) matches the entry
    baseline; absent from the baseline or changed since entry (a run-attributable
    rewrite, chmod, plant, or reversal), it is checked against ``resolved_roots``.
    """
    violations: List[str] = []

    # Scans tracked divergence AND untracked non-ignored files, so a sandbox bypass
    # PLANTING a brand-new untracked file is flagged, not only an edited tracked one.
    # path_inventory is the single NUL-safe lister (default core.quotePath safe).
    nonignored_after = path_inventory.list_working_tree_changed_paths(wt.repo_root, "HEAD")
    for relative in nonignored_after:
        # CONTENT-AND-MODE-based exemption (Round5 / Round7): a path already dirty at
        # entry is exempt only while its content AND on-disk mode match the entry
        # fingerprint; absent from the baseline, or content/permission-bit changed
        # since entry (a run-attributable rewrite or chmod), it is checked against roots.
        entry_fingerprint = baseline.get(relative)
        if entry_fingerprint is not None and _working_tree_fingerprint(wt.repo_root, relative) == entry_fingerprint:
            continue
        candidate = (wt.repo_root / relative).resolve()
        if not worktree._within_any(candidate, resolved_roots):
            violations.append(str(candidate))

    # Grok r5 #4: the scan above is blind to gitignored paths, so a sandbox bypass PLANTING
    # repo/.env.local or repo/node_modules/.bin/evil evaded it. The original checkout must be
    # UNTOUCHED, so the gitignored set is scanned too: an ignored path is exempt only while
    # its bounded stat signature matches the entry baseline; otherwise it is a violation.
    ignored_after = sorted(path_inventory.list_ignored_untracked_paths(wt.repo_root))
    for relative in ignored_after:
        entry_signature = baseline.get(relative)
        if entry_signature is not None and _ignored_path_signature(wt.repo_root, relative) == entry_signature:
            continue
        candidate = (wt.repo_root / relative).resolve()
        if not worktree._within_any(candidate, resolved_roots):
            violations.append(str(candidate))

    # PR968 codex #3 vanished-dirt: the scans above only iterate paths STILL dirty
    # after the run, so a run that DELETED a pre-existing untracked/ignored file or
    # RESTORED a pre-existing dirty tracked file to HEAD drops it from the after-set
    # and its baseline entry is never compared. Every baseline path absent from BOTH
    # after-scans is no longer dirty -- a run-attributable reversal of the operator's
    # own uncommitted work; outside the allowed roots it is a violation.
    after_paths = set(nonignored_after) | set(ignored_after)
    for relative in sorted(set(baseline) - after_paths):
        candidate = (wt.repo_root / relative).resolve()
        if not worktree._within_any(candidate, resolved_roots):
            violations.append(str(candidate))

    return violations


def _raise_escape_violations(
    function: str,
    message_template: str,
    violations: List[str],
    resolved_roots: List[pathlib.Path],
    extra_detail: Optional[Dict[str, str]] = None,
) -> None:
    """Fail closed as ``unexpected-edits`` when ``violations`` is non-empty; no-op otherwise.

    Shared by ``assert_changes_within`` and ``assert_original_checkout_unmodified``
    so both surface offending absolute paths identically (DRY). ``message_template``
    receives the comma-joined unique violations via ``str.format``.
    """
    if not violations:
        return
    unique_violations = sorted(set(violations))
    _log(function, "changes outside allowed roots: {}".format(unique_violations))
    detail: Dict[str, object] = {
        "violations": unique_violations,
        "allowedRoots": [str(root) for root in resolved_roots],
    }
    if extra_detail is not None:
        detail.update(extra_detail)
    raise GrokWrapperError(
        "unexpected-edits",
        message_template.format(", ".join(unique_violations)),
        detail,
    )


def assert_original_checkout_unmodified(
    wt: ExternalWorktree,
    allowed_roots: Tuple[pathlib.Path, ...],
    original_baseline: Optional[Dict[str, str]] = None,
) -> None:
    """Re-scan ONLY the operator's real checkout for escapes; the worktree's own outputs are never scanned.

    PR968 codex post-build-gate: the code-mode build gate runs Grok-MODIFIABLE pnpm
    scripts (build/typecheck/lint/test) in the UNSANDBOXED wrapper process AFTER the
    entry escape scan has already passed. A Grok-modified script that writes into the
    operator's REAL checkout would therefore go undetected. This re-runs the
    original-checkout comparison (the tracked/untracked/ignored/vanished surfaces of
    ``_collect_original_checkout_escapes``) so a build that mutated the real checkout
    is flagged fail-closed. It deliberately scans ONLY ``wt.repo_root``: the isolated
    worktree's own build outputs (dist/.next/node_modules under ``wt.path``) and
    global caches outside the checkout (``~/.pnpm-store``) are never inspected here,
    so legitimate build writes never false-positive.
    """
    resolved_roots = [pathlib.Path(root).resolve() for root in allowed_roots]
    baseline: Dict[str, str] = original_baseline if original_baseline is not None else {}
    violations = _collect_original_checkout_escapes(wt, resolved_roots, baseline)
    _raise_escape_violations(
        "assert_original_checkout_unmodified",
        "the build gate wrote outside the allowed roots: {}",
        violations,
        resolved_roots,
        extra_detail={"phase": "post-build-gate"},
    )
