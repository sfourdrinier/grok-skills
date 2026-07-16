# wrapper/scripts/groklib/review_isolation.py
#
# Opt-in isolated review worktrees (design §10, rev 9): only when --isolated.
# Path: {state_root}/worktrees/review/{run_id}; sibling owner marker; apply
# tracked dirty from the live checkout; cleanup always. Fail closed with
# isolation-unavailable — never silently fall back to the live tree.

from __future__ import annotations

import dataclasses
import os
import pathlib
import stat
import subprocess
from typing import List, Optional, Sequence

from groklib import GrokWrapperError, log_stderr, platformsupport, runstate
from groklib import worktree as worktree_mod

_BRANCH_PREFIX = "grok/review/"
_FILE_MODE = 0o600


def _log(function: str, message: str) -> None:
    log_stderr("review_isolation", function, message)


def _run_git_bytes(repo: pathlib.Path, args: Sequence[str]) -> "subprocess.CompletedProcess":
    """Run git capturing raw stdout bytes (no UTF-8 decode) for binary-safe patches.

    ``worktree._run_git`` decodes stdout as UTF-8 and raises UnicodeDecodeError on
    non-UTF-8 text hunks before a CompletedProcess exists; isolation patches must
    survive arbitrary tracked dirty bytes (design §10).
    """
    argv = [
        "git",
        "-c",
        "core.hooksPath={}".format(worktree_mod._EMPTY_GIT_HOOKS),
        "-C",
        str(repo),
    ] + [str(arg) for arg in args]
    try:
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=worktree_mod._git_env(None),
            timeout=worktree_mod._GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _iso_error(
            "git could not be executed for isolation patch: {}".format(exc),
            {"argv": argv},
        ) from exc


@dataclasses.dataclass(frozen=True)
class ReviewIsolation:
    repo_root: pathlib.Path
    worktree_path: pathlib.Path
    branch: str
    base_revision: str
    run_id: str
    diff_path: pathlib.Path
    marker_path: pathlib.Path


def _iso_error(message: str, detail: Optional[dict] = None) -> GrokWrapperError:
    return GrokWrapperError("isolation-unavailable", message, detail or {})


def _write_private_bytes(path: pathlib.Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        platformsupport.restrict_file_permissions(path)
    except Exception:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        raise


def _line_has_gitlink(line: str) -> bool:
    """True when a ``git diff --raw`` line involves mode 160000 (gitlink/submodule)."""
    # raw format: :oldmode newmode oldsha newsha status\tpath
    if not line.startswith(":"):
        return False
    parts = line[1:].split()
    if len(parts) < 2:
        return False
    return parts[0] == "160000" or parts[1] == "160000"


def _reject_dirty_submodules(repo_root: pathlib.Path) -> None:
    """Fail closed if any gitlink (submodule) differs from HEAD (design §10)."""
    for args in (("diff", "--raw", "HEAD"), ("diff", "--raw", "--cached", "HEAD")):
        completed = worktree_mod._run_git(repo_root, list(args))
        if completed.returncode not in (0, 1):
            raise _iso_error(
                "could not inspect repository for dirty submodules",
                {"stderr": (completed.stderr or "").strip(), "argv": list(args)},
            )
        for line in (completed.stdout or "").splitlines():
            if _line_has_gitlink(line):
                raise _iso_error(
                    "dirty or changed git submodules are not supported under --isolated",
                    {
                        "hint": "commit or clean submodule state, or run without --isolated",
                        "line": line,
                    },
                )


def _intent_to_add_paths(repo_root: pathlib.Path) -> List[str]:
    """Return paths marked intent-to-add (``git add -N``), excluded from isolation.

    Porcelain v2 reports ITA ordinary entries with both HEAD and index OIDs all
    zeros (design §10 / R5). ``--ita-invisible-in-index`` alone is insufficient
    on modern git for ``git diff HEAD`` (still emits ITA as new-file patches).
    """
    completed = worktree_mod._run_git(
        repo_root, ["status", "--porcelain=v2", "-z", "--untracked-files=no"]
    )
    if completed.returncode != 0:
        raise _iso_error(
            "could not list intent-to-add paths for isolation",
            {"stderr": (completed.stderr or "").strip()},
        )
    zero = "0" * 40
    paths: List[str] = []
    for entry in (completed.stdout or "").split("\0"):
        if not entry.startswith("1 "):
            continue
        # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
        parts = entry.split(" ", 8)
        if len(parts) < 9:
            continue
        head_oid, index_oid, path = parts[6], parts[7], parts[8]
        if head_oid == zero and index_oid == zero and path:
            paths.append(path)
    return paths


def prepare_review_isolation(*, repo_root: pathlib.Path, run_id: str) -> ReviewIsolation:
    """Create owned review worktree at HEAD, apply tracked dirty, return session.

    Raises GrokWrapperError(isolation-unavailable) on any setup failure after
    best-effort cleanup of partial state.
    """
    if not runstate.is_valid_run_id(run_id):
        raise _iso_error("run id is not valid for review isolation: {!r}".format(run_id), {"runId": run_id})

    resolved_repo = pathlib.Path(repo_root).resolve()
    worktree_path = runstate.state_root() / "worktrees" / "review" / run_id
    branch = _BRANCH_PREFIX + run_id
    marker_path = worktree_mod.marker_path_for(worktree_path)
    diff_path = pathlib.Path(str(worktree_path) + ".diff")

    if worktree_mod._is_within(worktree_path, resolved_repo):
        raise _iso_error(
            "state root places the review isolation worktree inside the target checkout",
            {"path": str(worktree_path), "repoRoot": str(resolved_repo)},
        )

    if worktree_path.exists() or marker_path.exists() or diff_path.exists():
        raise _iso_error(
            "review isolation path already exists; existing paths are never reused",
            {"path": str(worktree_path)},
        )
    if worktree_mod._branch_exists(resolved_repo, branch):
        raise _iso_error(
            "review isolation branch already exists; existing branches are never reused",
            {"branch": branch},
        )

    try:
        _reject_dirty_submodules(resolved_repo)
        base_sha = worktree_mod._resolve_base_sha(resolved_repo, "HEAD")
        worktree_mod._make_secure_dir(worktree_path.parent)
        worktree_mod._git(
            resolved_repo, "worktree", "add", "-b", branch, str(worktree_path), base_sha
        )
    except GrokWrapperError as exc:
        if exc.error_class == "isolation-unavailable":
            raise
        # Map worktree-failure from helpers into isolation-unavailable
        raise _iso_error(str(exc), dict(exc.detail or {}, mappedFrom=exc.error_class)) from exc
    except OSError as exc:
        raise _iso_error("could not create review isolation worktree: {}".format(exc)) from exc

    session = ReviewIsolation(
        repo_root=resolved_repo,
        worktree_path=worktree_path,
        branch=branch,
        base_revision=base_sha,
        run_id=run_id,
        diff_path=diff_path,
        marker_path=marker_path,
    )

    try:
        try:
            platformsupport.restrict_dir_permissions(worktree_path)
        except OSError as exc:
            raise _iso_error(
                "could not harden review isolation worktree permissions: {}".format(exc)
            ) from exc

        runstate.write_owner_marker_file(marker_path, run_id)

        # Tracked dirty (staged+unstaged). Keep --ita-invisible-in-index (design)
        # and exclude ITA paths explicitly so modern git cannot still emit them.
        ita_paths = _intent_to_add_paths(resolved_repo)
        diff_argv = [
            "diff",
            "--binary",
            "--full-index",
            "--ita-invisible-in-index",
            "HEAD",
            "--",
            ".",
        ]
        for ita in ita_paths:
            # pathspec exclude; leading ./ keeps paths under repo root
            diff_argv.append(":(exclude){}".format(ita))

        completed = _run_git_bytes(resolved_repo, diff_argv)
        if completed.returncode not in (0, 1):
            stderr_text = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            raise _iso_error(
                "could not generate isolation dirty patch",
                {"stderr": stderr_text},
            )
        patch_bytes = completed.stdout or b""
        _write_private_bytes(diff_path, patch_bytes)

        if patch_bytes.strip():
            apply = worktree_mod._run_git(
                worktree_path,
                ["apply", "--whitespace=nowarn", str(diff_path)],
            )
            if apply.returncode != 0:
                raise _iso_error(
                    "could not apply tracked dirty patch into isolation worktree",
                    {"stderr": (apply.stderr or "").strip()},
                )
    except BaseException:
        cleanup_review_isolation(session)
        raise

    return session


def cleanup_review_isolation(session: ReviewIsolation) -> None:
    """Best-effort always cleanup: worktree, branch, marker, diff (design §10)."""
    repo = session.repo_root
    path = session.worktree_path
    try:
        removed = worktree_mod._run_git(repo, ["worktree", "remove", "--force", str(path)])
        if removed.returncode != 0:
            worktree_mod._run_git(repo, ["worktree", "prune"])
            worktree_mod._run_git(repo, ["worktree", "remove", "--force", str(path)])
    except GrokWrapperError as exc:
        _log("cleanup_review_isolation", "worktree remove failed: {}".format(exc))

    try:
        if path.exists():
            import shutil

            shutil.rmtree(str(path), ignore_errors=True)
    except OSError as exc:
        _log("cleanup_review_isolation", "rmtree failed for {}: {}".format(path, exc))

    try:
        worktree_mod._run_git(repo, ["worktree", "prune"])
    except GrokWrapperError:
        pass

    try:
        worktree_mod._run_git(repo, ["branch", "-D", session.branch])
    except GrokWrapperError as exc:
        _log("cleanup_review_isolation", "branch delete failed: {}".format(exc))

    for p in (session.marker_path, session.diff_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log("cleanup_review_isolation", "could not remove {}: {}".format(p, exc))
