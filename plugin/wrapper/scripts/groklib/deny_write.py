# wrapper/scripts/groklib/deny_write.py
#
# Protected write-deny globs + match SSOT for direct finalize and (via Node
# mirror) auto/peer apply pre-block. Data lives in
# plugin/references/deny-write-globs.json - never hardcode a second list here.

from __future__ import annotations

import fnmatch
import json
import pathlib
from typing import List, Sequence, Tuple

# plugin/references/deny-write-globs.json relative to this module:
# groklib/ -> scripts/ -> wrapper/ -> plugin/ -> references/
_DATA_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "references"
    / "deny-write-globs.json"
)

_LOADED = False
_GLOBS: Tuple[str, ...] = ()


def _load_ssot() -> Tuple[str, ...]:
    global _LOADED, _GLOBS
    if _LOADED:
        return _GLOBS
    if not _DATA_PATH.is_file():
        raise RuntimeError(
            "deny-write SSOT missing at {}".format(_DATA_PATH)
        )
    with _DATA_PATH.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    globs = doc.get("globs")
    if not isinstance(globs, list) or not globs:
        raise RuntimeError("deny-write SSOT has empty/invalid globs")
    if not all(isinstance(g, str) and g for g in globs):
        raise RuntimeError("deny-write SSOT globs must be non-empty strings")
    _GLOBS = tuple(globs)
    _LOADED = True
    return _GLOBS


def deny_write_ssot_path() -> pathlib.Path:
    """Absolute path to the shared deny-write JSON SSOT (for tests/docs)."""
    return _DATA_PATH


def deny_write_globs() -> Tuple[str, ...]:
    """Return the deny-write glob tuple from the JSON SSOT (cached)."""
    return _load_ssot()


# Compat alias used by direct_finalize and historical imports.
# Populated eagerly so ``from groklib.deny_write import DENY_WRITE_GLOBS`` works
# and remains a tuple identical to the previous in-module constant.
DENY_WRITE_GLOBS: Tuple[str, ...] = deny_write_globs()


def posix_rel(path: str) -> str:
    """POSIX-normalize a repo-relative path WITHOUT stripping a leading dotfile.

    Do not use ``str.lstrip('./')``: that treats '.' as a character class and
    would turn ``.env`` into ``env`` and ``.git/config`` into ``git/config``.
    """
    norm = str(path or "").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def path_matches_deny(path: str, globs: Sequence[str] | None = None) -> bool:
    """True when a repo-relative path matches the deny-write globs or any .git.

    Algorithm (shared with Node deny-write.mjs / golden vectors):
      1. POSIX-normalize (strip leading ``./`` only).
      2. Empty path -> False.
      3. Any path component named ``.git`` -> True (root, nested vendor/lib/.git,
         modules).
      4. Else match each glob via fnmatch against full path OR basename
         (Python fnmatch: ``*`` matches ``/``).
    """
    patterns: Sequence[str] = _load_ssot() if globs is None else globs
    norm = posix_rel(path)
    if not norm:
        return False
    parts = [p for p in norm.split("/") if p]
    # Any path component named .git (root, nested vendor repo, submodule gitdir).
    if ".git" in parts:
        return True
    base = parts[-1] if parts else ""
    for pattern in patterns:
        if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(base, pattern):
            return True
    return False


def load_match_vectors() -> List[dict]:
    """Return matchVectors from the SSOT (for dual-host golden tests)."""
    with _DATA_PATH.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    vectors = doc.get("matchVectors") or []
    if not isinstance(vectors, list):
        raise RuntimeError("deny-write SSOT matchVectors must be a list")
    return vectors
