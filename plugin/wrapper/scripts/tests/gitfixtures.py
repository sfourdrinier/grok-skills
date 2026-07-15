# wrapper/scripts/tests/gitfixtures.py
#
# Real-git test fixture shared by every worktree/git test. make_repo builds an
# actual on-disk git repository (via subprocess argv-list git, never shell) with
# two commits, a nested pkg/ directory, and one dirty uncommitted file, so the
# worktree lifecycle is exercised against genuine git plumbing rather than a
# mock. Git identity is configured LOCALLY in the fixture repo (never relying on
# global git config) so commits succeed in CI.

import pathlib
import subprocess
from typing import Sequence, Union


class GitFixtureError(RuntimeError):
    """Raised when a git command used to build the fixture repo fails."""


def _git(repo: pathlib.Path, args: Sequence[str]) -> str:
    """Run one git command in ``repo`` via an argv list (never shell) and return stdout.

    Raises GitFixtureError with the command, exit status, and stderr on any
    non-zero exit or missing binary so a broken fixture fails loudly instead of
    silently producing a malformed repo.
    """
    argv = ["git", "-C", str(repo)] + [str(arg) for arg in args]
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        raise GitFixtureError("git could not be executed: {}: {}".format(argv, exc)) from exc
    if completed.returncode != 0:
        raise GitFixtureError(
            "git command failed ({}): exit {} stderr={!r}".format(
                " ".join(str(arg) for arg in args), completed.returncode, completed.stderr.strip()
            )
        )
    return completed.stdout


def make_repo(tmpdir: Union[str, pathlib.Path]) -> pathlib.Path:
    """Create a real git repo under ``tmpdir`` and return its absolute root path.

    The repo has two commits, a nested ``pkg/`` directory tracked from the first
    commit, and one dirty uncommitted file ``dirty.txt`` left untracked at HEAD.
    Callers own ``tmpdir`` cleanup (typically ``addCleanup(shutil.rmtree, ...)``).
    """
    repo_root = pathlib.Path(tmpdir) / "repo"
    repo_root.mkdir(parents=True, exist_ok=False)

    _git(repo_root, ["init", "-q"])
    # Local-only identity + signing config so commits work without any global
    # git configuration present on the host or CI runner.
    _git(repo_root, ["config", "user.name", "Grok CLI Test"])
    _git(repo_root, ["config", "user.email", "grok-cli-test@example.com"])
    _git(repo_root, ["config", "commit.gpgsign", "false"])

    (repo_root / "pkg").mkdir(parents=True, exist_ok=False)
    (repo_root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo_root / "pkg" / "mod.txt").write_text("module\n", encoding="utf-8")
    _git(repo_root, ["add", "-A"])
    _git(repo_root, ["commit", "-q", "-m", "commit one: initial tree"])

    with (repo_root / "a.txt").open("a", encoding="utf-8") as handle:
        handle.write("beta\n")
    _git(repo_root, ["add", "-A"])
    _git(repo_root, ["commit", "-q", "-m", "commit two: extend a.txt"])

    (repo_root / "dirty.txt").write_text("dirty and uncommitted\n", encoding="utf-8")

    return repo_root


def head_revision(repo_root: pathlib.Path) -> str:
    """Return the current HEAD commit sha of ``repo_root`` (helper for tests)."""
    return _git(repo_root, ["rev-parse", "HEAD"]).strip()
