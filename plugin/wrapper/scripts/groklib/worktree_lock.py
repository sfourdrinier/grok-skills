# wrapper/scripts/groklib/worktree_lock.py
#
# Per-repo exclusive lock for git worktree add/remove/prune. Concurrent
# `git worktree add` on one repository races inside .git/worktrees (commondir
# unread while a sibling is mid-create). Same-thread nesting is allowed so
# add-failure rollback can remove/prune without deadlocking.

from __future__ import annotations

import contextlib
import hashlib
import pathlib
import threading

from groklib.filelock import exclusive_file_lock

_TLS = threading.local()
_LOCK_NAME = "grok-skills-worktree.lock"


def repo_worktree_lock_path(repo: pathlib.Path) -> pathlib.Path:
    """Lock file for git worktree mutations of ``repo``.

    Prefers ``<repo>/.git/grok-skills-worktree.lock`` (the metadata being
    mutated). If ``.git`` is not a directory (linked worktree), fall back to
    a state-root lock keyed by the resolved repo path.
    """
    resolved = pathlib.Path(repo).resolve()
    git_dir = resolved / ".git"
    if git_dir.is_dir():
        return git_dir / _LOCK_NAME
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    from groklib import runstate

    return runstate.state_root() / "locks" / "worktree-{}.lock".format(digest)


@contextlib.contextmanager
def repo_worktree_lock(repo: pathlib.Path):
    """Serialize worktree add/remove/prune for one repository (reentrant)."""
    key = str(pathlib.Path(repo).resolve())
    held = getattr(_TLS, "held", None)
    if held is None:
        _TLS.held = set()
        held = _TLS.held
    if key in held:
        yield
        return
    held.add(key)
    try:
        with exclusive_file_lock(repo_worktree_lock_path(repo)):
            yield
    finally:
        held.discard(key)
