# wrapper/scripts/groklib/worktree.py
#
# External git-worktree lifecycle: the core safety boundary guaranteeing Grok
# writes only into an isolated worktree under the C2 state root and never touches
# the operator's real checkout. Every git call is an argv list through the private
# _git / _git_query helpers (never shell=True), every precondition fails closed,
# and the sibling ownership marker proves the worktree is ours before removal.
#
# The escape-detection + change-fingerprinting subsystem that consumes this module's
# git plumbing (assert_changes_within, capture_original_checkout_baseline,
# repo_change_fingerprint, assert_original_checkout_unmodified, and the artifact/
# fingerprint helpers) lives in groklib.worktree_escape (900-line cap split); it
# reaches back into worktree._git / _git_query / _within_any / diff_summary through
# the module, a strictly one-directional dependency.

import dataclasses
import os
import pathlib
import re
import subprocess
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import platformsupport, runstate

_BRANCH_PREFIX = "grok/code/"
# Run-bound branches that cleanup may delete (code + opt-in review isolation).
_RUN_BOUND_BRANCH_PREFIXES = (_BRANCH_PREFIX, "grok/review/")
_MARKER_SUFFIX = ".owner.json"
_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_GIT_TIMEOUT_SECONDS = 30
_REFS_HEADS_PREFIX = "refs/heads/"


@dataclasses.dataclass(frozen=True)
class ExternalWorktree:
    path: pathlib.Path
    branch: str
    base_revision: str
    repo_root: pathlib.Path


def rebuild_worktree_from_record(record: dict) -> Optional[ExternalWorktree]:
    """Rebuild an ExternalWorktree from a C2 run.json record, or None when fields are incomplete.

    Single source for cleanup and ``code --continue-run``: requires worktreePath,
    worktreeBranch, baseRevision, and repository as strings.
    """
    path = record.get("worktreePath")
    branch = record.get("worktreeBranch")
    base = record.get("baseRevision")
    repository = record.get("repository")
    if not (
        isinstance(path, str)
        and isinstance(branch, str)
        and isinstance(base, str)
        and isinstance(repository, str)
    ):
        return None
    return ExternalWorktree(
        path=pathlib.Path(path),
        branch=branch,
        base_revision=base,
        repo_root=pathlib.Path(repository),
    )


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "worktree" component prefix."""
    log_stderr("worktree", function, message)


# Empty hooks dir: disable repo hooks for every wrapper-driven git invocation
# (malicious post-checkout must not run as the operator during worktree create).
_EMPTY_GIT_HOOKS = pathlib.Path(__file__).resolve().parent / ".empty-git-hooks"
try:
    _EMPTY_GIT_HOOKS.mkdir(mode=0o700, exist_ok=True)
except OSError:
    pass


def _git_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Child env for git: scrub global/system config; keep caller overrides."""
    merged = dict(os.environ if env is None else env)
    merged.setdefault("GIT_CONFIG_GLOBAL", os.devnull)
    merged.setdefault("GIT_CONFIG_SYSTEM", os.devnull)
    return merged


def _run_git(
    repo: pathlib.Path, args: Sequence[str], env: Optional[Dict[str, str]] = None
) -> "subprocess.CompletedProcess":
    """Run a git command in ``repo`` as an argv list (never shell) and return the completed process.

    Raises GrokWrapperError("worktree-failure") only when the git binary itself
    cannot be executed (OSError). A non-zero exit is returned to the caller so
    boolean probes (ancestry, ref existence) can inspect the status; commands
    that must succeed go through _git, which raises on non-zero. ``env``, when
    supplied, replaces the child environment (used to redirect GIT_INDEX_FILE so
    a snapshot never mutates the real index); None inherits the parent env.
    Always disables hooks via core.hooksPath.
    """
    argv = [
        "git",
        "-c",
        "core.hooksPath={}".format(_EMPTY_GIT_HOOKS),
        "-C",
        str(repo),
    ] + [str(arg) for arg in args]
    try:
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            env=_git_env(env),
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # A bounded wall-clock timeout guards a hung git the same way the verify
        # mode's git helper did (T3 unified this source); a spawn failure or a
        # timeout is a fail-closed worktree-failure.
        _log("_run_git", "git could not be executed: {}: {}".format(argv, exc))
        raise GrokWrapperError(
            "worktree-failure",
            "git could not be executed: {}".format(exc),
            {"argv": argv},
        ) from exc


def _git(repo: pathlib.Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    """Run a git command that MUST succeed; return stdout, raise worktree-failure on non-zero."""
    completed = _run_git(repo, args, env=env)
    if completed.returncode != 0:
        argv = [
            "git",
            "-c",
            "core.hooksPath={}".format(_EMPTY_GIT_HOOKS),
            "-C",
            str(repo),
        ] + [str(arg) for arg in args]
        stderr = completed.stderr.strip()
        _log("_git", "git {} exited {}: {}".format(list(args), completed.returncode, stderr))
        raise GrokWrapperError(
            "worktree-failure",
            "git command failed: {}".format(" ".join(str(arg) for arg in args)),
            {"argv": argv, "exitStatus": completed.returncode, "stderr": stderr},
        )
    return completed.stdout


def git_checked(repo: pathlib.Path, *args: str) -> str:
    """Run a git command in ``repo`` that MUST succeed (argv list, bounded timeout); return stdout.

    The single must-succeed git runner for the worktree lifecycle, reused by
    verify mode (T3) instead of its own duplicate helper. Raises
    worktree-failure on a spawn failure, a timeout, or a non-zero exit.
    """
    return _git(repo, *args)


def parse_worktree_porcelain(porcelain: str) -> List[Tuple[pathlib.Path, Optional[str]]]:
    """Parse `git worktree list --porcelain` into (resolved path, short-branch-name-or-None) entries.

    The single porcelain parser for the worktree lifecycle (T3): verify mode
    reuses this instead of a second, divergent parser. The branch is the short
    name (``refs/heads/`` stripped), or None for a detached/branchless entry.
    """
    entries: List[Tuple[pathlib.Path, Optional[str]]] = []
    current_path: Optional[pathlib.Path] = None
    current_branch: Optional[str] = None

    def _flush() -> None:
        if current_path is not None:
            entries.append((current_path, current_branch))

    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            _flush()
            current_path = pathlib.Path(line[len("worktree "):]).resolve()
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            current_branch = ref[len(_REFS_HEADS_PREFIX):] if ref.startswith(_REFS_HEADS_PREFIX) else ref
        elif line == "":
            _flush()
            current_path = None
            current_branch = None
    _flush()
    return entries


def _git_query(repo: pathlib.Path, *args: str) -> "subprocess.CompletedProcess":
    """Run a git command whose non-zero exit is a meaningful boolean signal, not an error."""
    return _run_git(repo, args)


def _repo_slug(repo_root: pathlib.Path) -> str:
    """Derive a filesystem-safe <repo-name> slug from the repo basename (D-PORT).

    Any character outside [A-Za-z0-9._-] is replaced with '_', so the slug is
    safe on POSIX and Windows filesystems alike. Empty or dot-only basenames
    fall back to "repo"; the run id under it guarantees uniqueness regardless.
    """
    name = pathlib.Path(repo_root).resolve().name
    slug = _SLUG_UNSAFE.sub("_", name)
    if not slug or slug in (".", ".."):
        return "repo"
    return slug


def marker_path_for(worktree_path: pathlib.Path) -> pathlib.Path:
    """Return the sibling ownership-marker path (<worktree-path>.owner.json), never inside the worktree.

    The single source of the sibling-marker path convention, reused by verify
    mode (T3) instead of re-deriving the ``.owner.json`` suffix locally.
    """
    return pathlib.Path(str(worktree_path) + _MARKER_SUFFIX)


def _marker_path(worktree_path: pathlib.Path) -> pathlib.Path:
    """Internal alias for ``marker_path_for`` used by this module's own call sites."""
    return marker_path_for(worktree_path)


def _make_secure_dir(path: pathlib.Path) -> None:
    """Create ``path`` and any missing ancestors, hardening each newly created directory to owner-only.

    Mirrors the C2 "all directories created with mode 0700" rule (POSIX chmod /
    Windows ACL, routed through platformsupport) for every level this call
    materializes, since mkdir(parents=True, mode=...) does not apply the mode to
    intermediate parents.
    """
    to_create: List[pathlib.Path] = []
    current = path
    while not current.exists():
        to_create.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for directory in reversed(to_create):
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            platformsupport.restrict_dir_permissions(directory)
        except OSError as exc:
            _log("_make_secure_dir", "failed to create {}: {}".format(directory, exc))
            raise GrokWrapperError(
                "worktree-failure",
                "failed to create worktree parent directory {}: {}".format(directory, exc),
                {"path": str(directory)},
            ) from exc


def _resolve_base_sha(repo_root: pathlib.Path, base: str) -> str:
    """Resolve ``base`` to a concrete commit sha, failing closed if it does not resolve."""
    completed = _git_query(repo_root, "rev-parse", "--verify", "{}^{{commit}}".format(base))
    if completed.returncode != 0:
        _log("_resolve_base_sha", "base {!r} did not resolve to a commit".format(base))
        raise GrokWrapperError(
            "worktree-failure",
            "base revision does not resolve to a commit: {}".format(base),
            {"base": base, "stderr": completed.stderr.strip()},
        )
    return completed.stdout.strip()


def _branch_exists(repo_root: pathlib.Path, branch: str) -> bool:
    completed = _git_query(repo_root, "show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch))
    return completed.returncode == 0


def _is_within(child: pathlib.Path, root: pathlib.Path) -> bool:
    """True when ``child`` is ``root`` itself or nested under ``root`` (realpath-normalized)."""
    resolved_child = child.resolve()
    resolved_root = root.resolve()
    return resolved_child == resolved_root or resolved_root in resolved_child.parents


def _within_any(child: pathlib.Path, roots: Sequence[pathlib.Path]) -> bool:
    return any(_is_within(child, root) for root in roots)


def create_external_worktree(*, repo_root: pathlib.Path, base: str, run_id: str) -> ExternalWorktree:
    """Create the C2 external worktree for ``run_id`` and its sibling ownership marker.

    Path is state_root()/worktrees/<repo-name>/<run-id> (never under repo_root);
    branch is grok/code/<run-id>; base is resolved to a concrete sha. A collision
    of either the path or the branch, an unresolvable base, or a malformed run id
    is a fail-closed worktree-failure. The ownership marker is written NEXT TO the
    worktree (<path>.owner.json) via runstate.write_owner_marker_file only after
    git has created the worktree.
    """
    if not runstate.is_valid_run_id(run_id):
        _log("create_external_worktree", "rejected malformed run id {!r}".format(run_id))
        raise GrokWrapperError(
            "worktree-failure",
            "run id is not valid for worktree creation: {!r}".format(run_id),
            {"runId": run_id},
        )

    resolved_repo_root = pathlib.Path(repo_root).resolve()
    base_sha = _resolve_base_sha(resolved_repo_root, base)
    slug = _repo_slug(resolved_repo_root)
    worktree_path = runstate.state_root() / "worktrees" / slug / run_id
    branch = _BRANCH_PREFIX + run_id
    marker_path = _marker_path(worktree_path)

    # PR968 codex state-root-in-checkout: the C2 worktree must be genuinely
    # EXTERNAL. If XDG_STATE_HOME resolves under the target checkout, the derived
    # worktree path lands inside the real repo; git would happily `worktree add`
    # nested there, dirtying the operator's checkout BEFORE any escape scan runs.
    # Fail closed here, in preparation, before the nested worktree is created.
    if _is_within(worktree_path, resolved_repo_root):
        _log(
            "create_external_worktree",
            "state root places worktree {} inside checkout {}".format(worktree_path, resolved_repo_root),
        )
        raise GrokWrapperError(
            "worktree-failure",
            "the resolved state root places the external worktree inside the target checkout; "
            "set XDG_STATE_HOME to a location outside the repository",
            {"path": str(worktree_path), "repoRoot": str(resolved_repo_root)},
        )

    if worktree_path.exists() or marker_path.exists():
        _log("create_external_worktree", "path collision at {}".format(worktree_path))
        raise GrokWrapperError(
            "worktree-failure",
            "worktree path already exists; existing paths are never reused: {}".format(worktree_path),
            {"path": str(worktree_path)},
        )
    if _branch_exists(resolved_repo_root, branch):
        _log("create_external_worktree", "branch collision at {}".format(branch))
        raise GrokWrapperError(
            "worktree-failure",
            "worktree branch already exists; existing branches are never reused: {}".format(branch),
            {"branch": branch},
        )

    _make_secure_dir(worktree_path.parent)
    _git(resolved_repo_root, "worktree", "add", "-b", branch, str(worktree_path), base_sha)
    # From here the git worktree (and its branch grok/code/<run-id>) EXIST. Any
    # failure setting them up (permission hardening, marker write) must NOT leave
    # a half-registered worktree/branch behind that cleanup cannot safely adopt
    # (no valid marker) -- remove BOTH before re-raising (Grok dogfood #6). Only
    # after the marker is written is the ExternalWorktree returned.
    try:
        try:
            platformsupport.restrict_dir_permissions(worktree_path)
        except OSError as exc:
            # The worktree exists; hardening its mode is defense in depth. Fail
            # closed rather than leave a wrongly-permissioned worktree registered.
            _log("create_external_worktree", "failed to harden worktree dir {}: {}".format(worktree_path, exc))
            raise GrokWrapperError(
                "worktree-failure",
                "failed to restrict worktree directory permissions: {}".format(exc),
                {"path": str(worktree_path)},
            ) from exc

        runstate.write_owner_marker_file(marker_path, run_id)
    except BaseException as exc:
        _log(
            "create_external_worktree",
            "setup failed after worktree add; removing orphaned worktree {} and branch {}: {}".format(
                worktree_path, branch, exc
            ),
        )
        stranded = _remove_partial_worktree(resolved_repo_root, worktree_path, branch, marker_path, run_id)
        if stranded:
            # PR968 codex record-partial-worktree: rollback could NOT remove the
            # just-added worktree, so it (and its grok/code/<run-id> branch) survive,
            # marker-recorded on disk. Attach the worktree identity to the raised
            # error so the caller records it into the run record BEFORE re-raising;
            # otherwise run.json carries no worktree fields and a later
            # `cleanup --run-id` removes only the run dir, stranding the worktree +
            # branch as an unreapable orphan. The identity is run-bound (path name
            # and branch both carry run_id), so cleanup reaps only its own.
            _attach_stranded_worktree(
                exc,
                ExternalWorktree(
                    path=worktree_path,
                    branch=branch,
                    base_revision=base_sha,
                    repo_root=resolved_repo_root,
                ),
            )
        raise

    return ExternalWorktree(
        path=worktree_path,
        branch=branch,
        base_revision=base_sha,
        repo_root=resolved_repo_root,
    )


_STRANDED_WORKTREE_ATTR = "grok_stranded_worktree"


def _attach_stranded_worktree(exc: BaseException, worktree: ExternalWorktree) -> None:
    """Annotate ``exc`` with a worktree rollback could not remove, so callers can enroll it for cleanup."""
    try:
        setattr(exc, _STRANDED_WORKTREE_ATTR, worktree)
    except (AttributeError, TypeError) as set_exc:
        # A slotted builtin exception could reject arbitrary attributes; the
        # worktree remains marker-recorded on disk (reapable by a marker audit),
        # so a failed best-effort annotation is logged, never fatal.
        _log(
            "_attach_stranded_worktree",
            "could not annotate error with stranded worktree {}: {}".format(worktree.path, set_exc),
        )


def stranded_worktree_from_error(exc: BaseException) -> Optional[ExternalWorktree]:
    """Return the stranded ExternalWorktree ``create_external_worktree`` attached to ``exc``, else None."""
    candidate = getattr(exc, _STRANDED_WORKTREE_ATTR, None)
    return candidate if isinstance(candidate, ExternalWorktree) else None


def _remove_partial_worktree(
    repo_root: pathlib.Path,
    worktree_path: pathlib.Path,
    branch: str,
    marker_path: pathlib.Path,
    run_id: str,
) -> bool:
    """Best-effort removal of a just-added worktree, its branch, and any partial marker.

    Returns True when the worktree could NOT be removed and therefore remains
    STRANDED (marker-recorded for later cleanup); False when it was fully removed.

    Runs only on the failure path of ``create_external_worktree`` (setup failed
    AFTER ``git worktree add`` succeeded). Never raises: it must not mask the
    original construction error that triggered it. Uses ``--force`` / ``-D``
    because the worktree may be non-empty and the branch was just created and
    holds no wanted commits at this point.

    Round4 F3-worktree-orphan: if the ``git worktree remove --force`` fails, it is
    retried once after a ``git worktree prune`` (clearing stale admin entries). If
    the worktree STILL cannot be removed, a valid owner marker is WRITTEN next to
    it so the worktree/branch are RECORDED for cleanup to reap later
    (``remove_external_worktree`` requires a verified marker) -- instead of being
    left permanently registered with no marker and thus unidentifiable and
    unreapable. The branch is only deleted once the worktree is gone (git refuses
    to delete a branch checked out at a still-registered worktree).
    """
    removed = _git_query(repo_root, "worktree", "remove", "--force", str(worktree_path))
    if removed.returncode != 0:
        _log(
            "_remove_partial_worktree",
            "git worktree remove --force {} exited {}: {}; pruning and retrying".format(
                worktree_path, removed.returncode, removed.stderr.strip()
            ),
        )
        _git_query(repo_root, "worktree", "prune")
        removed = _git_query(repo_root, "worktree", "remove", "--force", str(worktree_path))
        if removed.returncode != 0:
            _log(
                "_remove_partial_worktree",
                "retry of git worktree remove --force {} exited {}: {}".format(
                    worktree_path, removed.returncode, removed.stderr.strip()
                ),
            )

    worktree_gone = removed.returncode == 0 and not worktree_path.exists()
    if not worktree_gone:
        # Could not remove the worktree. Make it REAPABLE by cleanup: write a
        # valid owner marker so remove_external_worktree / the marker-based audit
        # can later identify and reap it, rather than leaving a permanent,
        # unidentifiable orphan with no marker (F3-worktree-orphan). The branch is
        # intentionally left in place (git refuses to delete a branch checked out
        # at a still-registered worktree); cleanup reaps both once it removes the
        # worktree with --force.
        try:
            runstate.write_owner_marker_file(marker_path, run_id)
            _log(
                "_remove_partial_worktree",
                "worktree {} could not be removed; recorded an owner marker so cleanup can reap it".format(
                    worktree_path
                ),
            )
        except OSError as exc:
            _log(
                "_remove_partial_worktree",
                "could not record owner marker for unremovable worktree {}: {}".format(worktree_path, exc),
            )
        return True

    # The worktree is gone: delete its just-created branch and any residual marker.
    branch_deleted = _git_query(repo_root, "branch", "-D", branch)
    if branch_deleted.returncode != 0:
        _log(
            "_remove_partial_worktree",
            "git branch -D {} exited {}: {}".format(branch, branch_deleted.returncode, branch_deleted.stderr.strip()),
        )

    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log("_remove_partial_worktree", "could not remove partial marker {}: {}".format(marker_path, exc))
    return False


def verify_external_worktree(wt: ExternalWorktree) -> None:
    """Verify ``wt`` is a registered worktree AND lives outside repo_root; else worktree-failure.

    Confirms `git worktree list --porcelain` contains an entry whose worktree
    path matches wt.path and whose branch matches refs/heads/<wt.branch>, then
    confirms wt.path is NOT nested inside repo_root (the external-isolation
    guarantee). Either failure is a fail-closed worktree-failure.
    """
    porcelain = _git(wt.repo_root, "worktree", "list", "--porcelain")
    target_path = wt.path.resolve()

    found = any(
        entry_path == target_path and entry_branch == wt.branch
        for entry_path, entry_branch in parse_worktree_porcelain(porcelain)
    )

    if not found:
        _log("verify_external_worktree", "no registered worktree for {} @ {}".format(target_path, wt.branch))
        raise GrokWrapperError(
            "worktree-failure",
            "worktree is not registered with matching path and branch: {}".format(wt.path),
            {"path": str(wt.path), "branch": wt.branch},
        )

    if _is_within(wt.path, wt.repo_root):
        _log("verify_external_worktree", "worktree {} is inside repo root {}".format(target_path, wt.repo_root))
        raise GrokWrapperError(
            "worktree-failure",
            "worktree path is inside the repository checkout; it must be external: {}".format(wt.path),
            {"path": str(wt.path), "repoRoot": str(wt.repo_root)},
        )


def diff_summary(wt: ExternalWorktree) -> Tuple[List[str], str]:
    """Return (changed worktree-relative paths incl. untracked, `git diff --stat` text vs base)."""
    tracked = _git(wt.path, "diff", "--name-only", wt.base_revision).splitlines()
    untracked = _git(wt.path, "ls-files", "--others", "--exclude-standard").splitlines()
    changed = sorted({entry for entry in (tracked + untracked) if entry})
    stat_text = _git(wt.path, "diff", "--stat", wt.base_revision)
    return changed, stat_text


def capture_worktree_snapshot(wt: ExternalWorktree) -> str:
    """Write a tree object capturing the worktree's FULL working-tree state and return its sha.

    A throwaway GIT_INDEX_FILE is used so `git add -A -f` (staging every tracked
    modification, deletion, and untracked path -- INCLUDING gitignored paths, via -f)
    and the subsequent `git write-tree` never mutate the operator's real index or working
    tree. The -f is load-bearing (PR968 codex verify-snapshot-ignored): a plain `git add
    -A` omits gitignored files, so a verify command writing a gitignored NON-artifact
    (e.g. .env.local) would produce NO diff path and slip the post-run change gate. With
    -f the snapshot captures gitignored writes too, and diff_since_snapshot surfaces them
    so assert_changes_within can flag or tolerate each per the artifact rule. The result is
    an absolute snapshot of the FULL working tree's content (ignored files included), used
    as verify's change-confinement base: a prior code run's uncommitted edits present at
    verify entry are captured here and never misattributed to verify. The temp index is
    always removed.
    """
    fd, tmp_index = tempfile.mkstemp(prefix="grok-worktree-index-")
    os.close(fd)
    # git treats a missing GIT_INDEX_FILE as an empty index; removing the just
    # created temp file gives `git add -A -f` a clean slate that mirrors the full
    # working tree rather than diffing against a seeded index.
    try:
        os.remove(tmp_index)
    except OSError as exc:
        _log("capture_worktree_snapshot", "could not clear temp index {}: {}".format(tmp_index, exc))
        raise GrokWrapperError(
            "worktree-failure",
            "could not prepare a snapshot index for {}".format(wt.path),
            {"path": str(wt.path)},
        ) from exc
    child_env = dict(os.environ)
    child_env["GIT_INDEX_FILE"] = tmp_index
    try:
        _git(wt.path, "add", "-A", "-f", env=child_env)
        tree = _git(wt.path, "write-tree", env=child_env).strip()
    finally:
        try:
            if os.path.exists(tmp_index):
                os.remove(tmp_index)
        except OSError as exc:
            _log("capture_worktree_snapshot", "could not remove temp index {}: {}".format(tmp_index, exc))
    if not tree:
        raise GrokWrapperError(
            "worktree-failure",
            "could not capture a worktree snapshot tree for {}".format(wt.path),
            {"path": str(wt.path)},
        )
    return tree


def diff_since_snapshot(wt: ExternalWorktree, entry_tree: str) -> Tuple[List[str], str]:
    """Return (changed worktree-relative paths, `git diff --stat` text) between ``entry_tree`` and now.

    The worktree's CURRENT full state is captured as a second snapshot tree so
    untracked files created during the run -- including gitignored ones (both snapshots
    stage with ``git add -A -f``) -- are compared on equal footing with tracked edits; the
    two trees are diffed by name, so the result is exactly what changed while the run
    executed (verify's own delta), not what it inherited from a prior code run. A write to
    a gitignored path therefore appears here; assert_changes_within then tolerates it ONLY
    when it is both gitignored AND under a build-artifact dir, flagging e.g. .env.local.
    """
    exit_tree = capture_worktree_snapshot(wt)
    names = _git(wt.path, "diff", "--name-only", entry_tree, exit_tree).splitlines()
    changed = sorted({entry for entry in names if entry})
    stat_text = _git(wt.path, "diff", "--stat", entry_tree, exit_tree)
    return changed, stat_text


def assert_committed_base_sufficient(
    repo_root: pathlib.Path, base: str, required_paths: Tuple[str, ...]
) -> None:
    """Verify ``base`` resolves, is an ancestor-of-or-equal-to HEAD, and contains every required path.

    Fails closed (worktree-failure) when the base does not resolve, is not an
    ancestor of HEAD, or does not contain a required path -- the spec 5.3
    uncommitted-state guarantee: the wrapper never stashes, commits, copies, or
    approximates uncommitted current-checkout changes.
    """
    resolved_repo_root = pathlib.Path(repo_root).resolve()
    base_sha = _resolve_base_sha(resolved_repo_root, base)

    ancestor = _git_query(resolved_repo_root, "merge-base", "--is-ancestor", base_sha, "HEAD")
    if ancestor.returncode not in (0, 1):
        _log(
            "assert_committed_base_sufficient",
            "merge-base --is-ancestor errored ({}): {}".format(ancestor.returncode, ancestor.stderr.strip()),
        )
        raise GrokWrapperError(
            "worktree-failure",
            "could not determine whether base {} is an ancestor of HEAD".format(base),
            {"base": base, "stderr": ancestor.stderr.strip()},
        )
    if ancestor.returncode == 1:
        _log("assert_committed_base_sufficient", "base {} is not an ancestor of HEAD".format(base_sha))
        raise GrokWrapperError(
            "worktree-failure",
            "committed base {} is not an ancestor of HEAD; refusing to build on divergent history".format(base),
            {"base": base, "baseSha": base_sha},
        )

    for required in required_paths:
        present = _git_query(resolved_repo_root, "cat-file", "-e", "{}:{}".format(base_sha, required))
        if present.returncode != 0:
            _log(
                "assert_committed_base_sufficient",
                "required path {!r} absent from committed base {}".format(required, base_sha),
            )
            raise GrokWrapperError(
                "worktree-failure",
                (
                    "the committed base {} does not contain required path {!r}; the wrapper does not "
                    "stash, commit, copy, or approximate uncommitted current-checkout changes".format(
                        base, required
                    )
                ),
                {"base": base, "baseSha": base_sha, "requiredPath": required},
            )


def remove_external_worktree(wt: ExternalWorktree, *, confirmed: bool, expected_run_id: str) -> dict:
    """Remove ``wt`` and its branch after PROVING OWNERSHIP; a valid marker is the gate, not cleanliness.

    Verifies the sibling ownership marker FIRST and refuses (fail closed) any
    worktree WITHOUT a valid owner marker. When not confirmed, returns a dry-run
    report and removes nothing. When confirmed, the worktree is removed regardless
    of dirty state (Grok dogfood-2 #8): code mode INTENTIONALLY leaves its worktree
    dirty, so refusing dirty would wedge the common success-case cleanup. Removal
    uses ``git worktree remove --force``; the marker plus ``--confirm`` are the
    authority. The branch is deleted with ``git branch -d`` (no ``-D``): if that
    fails (e.g. Grok made unmerged commits), the branch is RETAINED rather than
    raising after the worktree is gone -- report fields ``branchRetained``/
    ``branchRetainReason`` make the residual observable. The sibling marker is
    deleted last (best effort).

    PR968 codex #4 cleanup-binding: the marker AND the worktree dir name must BOTH
    equal ``expected_run_id`` (the REQUESTED run). A stale/corrupt run.json can
    point run A's record at run B's worktree; checking only ``wt.path.name`` would
    read B's own valid marker (id B == dir name B) and destroy B. Binding to the
    requested run id authorizes removal for that exact run only.

    PR968 codex bind-branch-deletion: branch removal is bound the same way. ``wt.branch``
    is read from run.json and is NOT trusted; only ``grok/code/<expected_run_id>`` is
    ever deleted. A record that pairs the correct owner-marked worktree path with an
    unrelated ``worktreeBranch`` (e.g. a merged branch) does NOT get that branch
    deleted -- it is retained and reported (``branchRetained``/``branchRetainReason``),
    fail-closed, so cleanup never destroys a branch that is not this run's.

    PR968 codex cleanup-retryable: a prior confirmed removal can reap BOTH the
    worktree dir and its sibling marker, then have a LATER step fail (the caller's
    run-dir delete), leaving run.json pointing at an already-gone worktree. On the
    retry the sibling marker is absent, so verifying it would raise
    state-ownership-violation and wedge the run dir forever. When both are already
    gone there is nothing left to destroy and no sibling marker to verify -- the
    removal authority came from the run-dir owner marker the cleanup caller already
    verified before calling here -- so the retry binds to the requested run id via
    the worktree path name (no marker file needed) and reports already-removed.
    """
    marker_path = _marker_path(wt.path)
    worktree_run_id = wt.path.name
    worktree_missing = not wt.path.exists()
    already_reaped = worktree_missing and not marker_path.exists()
    if already_reaped:
        # No sibling marker survives; the path name is the only binding available and
        # the caller already proved run ownership via the run-dir marker. Enforce the
        # path-name binding (a stale record pointing at a foreign path is still refused).
        owner_run_id = worktree_run_id
    else:
        owner_run_id = runstate.verify_owner_marker(marker_path)
    if owner_run_id != expected_run_id or worktree_run_id != expected_run_id:
        _log(
            "remove_external_worktree",
            "marker {!r} / worktree {!r} do not both match requested run id {!r}".format(
                owner_run_id, worktree_run_id, expected_run_id
            ),
        )
        raise GrokWrapperError(
            "state-ownership-violation",
            "ownership marker run id does not match the requested run: {}".format(wt.path),
            {"markerRunId": owner_run_id, "worktreeRunId": worktree_run_id, "requestedRunId": expected_run_id},
        )

    # Grok dogfood-4 #1 cleanup-wedge: when the worktree DIRECTORY is already gone
    # (the operator removed it, or a crash landed after `git worktree remove` but
    # before the run-dir delete) but the verified sibling marker remains, treat it
    # as ALREADY-REMOVED and proceed to reap the branch, marker, and (via the
    # caller) the run dir -- instead of raising worktree-failure and permanently
    # wedging cleanup so runs/<id>/ (with its progress.jsonl) can never be deleted.
    dirty = False if worktree_missing else bool(_git(wt.path, "status", "--porcelain").strip())
    report = {
        "confirmed": confirmed,
        "removed": False,
        "worktreePath": str(wt.path),
        "worktreeBranch": wt.branch,
        "baseRevision": wt.base_revision,
        "markerPath": str(marker_path),
        "dirty": dirty,
        "worktreeMissing": worktree_missing,
    }

    if not confirmed:
        return report

    if worktree_missing:
        # Already gone: there is nothing to `git worktree remove`. Prune any stale
        # git worktree registration for the vanished path so the just-created
        # branch is no longer considered checked out and can be deleted below.
        _log("remove_external_worktree", "worktree path already missing; pruning registration: {}".format(wt.path))
        _git_query(wt.repo_root, "worktree", "prune")
    else:
        # Ownership is proven (verified marker above) and removal is explicitly
        # confirmed: reap the worktree regardless of dirty state. code mode leaves
        # the worktree dirty by design, so --force is required to remove the common
        # success case (Grok dogfood-2 #8); refusing dirty would wedge cleanup.
        _git(wt.repo_root, "worktree", "remove", "--force", str(wt.path))

    branch_retained = False
    branch_retain_reason = None
    # Bind branch deletion to this run id only. Accept either code or review
    # isolation prefixes (``grok/code/<id>`` / ``grok/review/<id>``); never delete
    # an unrelated recorded branch name from a stale/corrupt run.json.
    expected_branches = {prefix + expected_run_id for prefix in _RUN_BOUND_BRANCH_PREFIXES}
    if wt.branch not in expected_branches:
        branch_retained = True
        branch_retain_reason = (
            "recorded branch {!r} is not a run-bound branch for run id {!r} "
            "(expected one of {})".format(wt.branch, expected_run_id, sorted(expected_branches))
        )
        _log(
            "remove_external_worktree",
            "refusing to delete non-run-bound branch {!r} (expected one of {})".format(
                wt.branch, sorted(expected_branches)
            ),
        )
    elif not _branch_exists(wt.repo_root, wt.branch):
        # The branch is already gone (e.g. a prior cleanup attempt deleted it before a
        # LATER step failed and the caller retried). Nothing to delete and nothing to
        # retain: attempting `git branch -d` on a missing branch would error and be
        # mis-reported as branchRetained.
        _log("remove_external_worktree", "branch {} already absent; nothing to delete".format(wt.branch))
    else:
        try:
            _git(wt.repo_root, "branch", "-d", wt.branch)
        except GrokWrapperError as exc:
            # The worktree directory is already gone at this point (git worktree
            # remove above succeeded), so re-raising here would leave the branch
            # and the sibling marker stranded with no way to retry: a retry hits
            # the "worktree path does not exist" guard earlier in this function
            # and the run is wedged. The branch is intentionally NOT force-deleted
            # (no -D) -- its commits, made by Grok inside the worktree, are
            # preserved for the operator -- so this is a documented, observable
            # retain, not a swallowed error or a weakened guard.
            branch_retained = True
            stderr = str(exc.detail.get("stderr", "")).strip()
            reason_source = stderr if stderr else str(exc)
            branch_retain_reason = reason_source.splitlines()[0] if reason_source else "branch delete failed"
            _log(
                "remove_external_worktree",
                "branch {} could not be deleted after worktree removal; retaining it: {}".format(
                    wt.branch, branch_retain_reason
                ),
            )

    marker_removed = True
    try:
        marker_path.unlink()
    except OSError as exc:
        # Worktree (and branch, when deletable) are already gone; a stranded
        # marker is residual state, not a removal failure. Log it and record it
        # in the report.
        _log("remove_external_worktree", "could not remove sibling marker {}: {}".format(marker_path, exc))
        marker_removed = False

    # Review isolation may leave a sibling ``{worktree}.diff`` (tracked-dirty
    # patch with source contents). Code mode never writes it; best-effort unlink
    # so cleanup --confirm cannot leave secrets after a crash-left isolation.
    diff_path = pathlib.Path(str(wt.path) + ".diff")
    diff_removed = True
    try:
        diff_path.unlink()
    except FileNotFoundError:
        diff_removed = False  # absent is normal for code worktrees
    except OSError as exc:
        _log(
            "remove_external_worktree",
            "could not remove sibling isolation patch {}: {}".format(diff_path, exc),
        )
        diff_removed = False

    report["removed"] = True
    report["markerRemoved"] = marker_removed
    report["diffPath"] = str(diff_path)
    report["diffRemoved"] = diff_removed
    report["branchRetained"] = branch_retained
    report["branchRetainReason"] = branch_retain_reason
    return report
