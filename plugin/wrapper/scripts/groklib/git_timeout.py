# wrapper/scripts/groklib/git_timeout.py
#
# Single source of truth for wrapper-driven git subprocess wall-clock timeouts.
# Default is monorepo-safe (issue #7/#8); override via env. Split from worktree.py
# to keep that module under the 900-line packaging cap.

from __future__ import annotations

import os

# Default git subprocess wall-clock for inventory / worktree prep / repo-root.
# 30s was too tight for large monorepos (issue #7/#8); 10 minutes is anti-hang,
# not anti-monorepo. Override: GROK_WRAPPER_GIT_TIMEOUT_SECONDS (clamped).
_DEFAULT_GIT_TIMEOUT_SECONDS = 600
_MIN_GIT_TIMEOUT_SECONDS = 30
_MAX_GIT_TIMEOUT_SECONDS = 7200


def git_timeout_seconds() -> int:
    """Wall-clock seconds for wrapper-driven git (SSOT for worktree + modes).

    Reads ``GROK_WRAPPER_GIT_TIMEOUT_SECONDS`` when set to a positive int;
    otherwise ``_DEFAULT_GIT_TIMEOUT_SECONDS``. Clamped to
    [``_MIN_GIT_TIMEOUT_SECONDS``, ``_MAX_GIT_TIMEOUT_SECONDS``].
    """
    raw = os.environ.get("GROK_WRAPPER_GIT_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw, 10)
        except ValueError:
            value = _DEFAULT_GIT_TIMEOUT_SECONDS
    else:
        value = _DEFAULT_GIT_TIMEOUT_SECONDS
    if value < _MIN_GIT_TIMEOUT_SECONDS:
        return _MIN_GIT_TIMEOUT_SECONDS
    if value > _MAX_GIT_TIMEOUT_SECONDS:
        return _MAX_GIT_TIMEOUT_SECONDS
    return value
