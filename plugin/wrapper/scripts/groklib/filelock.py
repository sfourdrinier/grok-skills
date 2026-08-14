# wrapper/scripts/groklib/filelock.py
#
# Exclusive file lock (fcntl on Unix, msvcrt on Windows). Single source for
# run.lock and repo worktree mutation locks.

from __future__ import annotations

import contextlib
import os
import pathlib

from groklib import platformsupport

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore

_FILE_MODE = 0o600


@contextlib.contextmanager
def exclusive_file_lock(lock_path: pathlib.Path):
    """Exclusive lock on ``lock_path`` (created 0600 if missing)."""
    path = pathlib.Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, _FILE_MODE)
    try:
        try:
            platformsupport.restrict_file_permissions(path)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)
