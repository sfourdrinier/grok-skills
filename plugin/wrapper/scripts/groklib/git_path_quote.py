# wrapper/scripts/groklib/git_path_quote.py
#
# Git core.quotePath C-style path decoding for non -z outputs (diff --git headers,
# apply --numstat, etc.). Distinct from path_inventory: NUL-safe -z inventories
# are already raw and must never be C-unquoted. Shared golden vectors:
# plugin/references/git-c-quoted-path-vectors.json (parity with Node unquoteGitPath).
# Invalid UTF-8 after decode uses U+FFFD (errors="replace") to match Node Buffer.

from __future__ import annotations

from typing import Optional, Tuple

_GIT_C_NAMED_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


def decode_git_c_escape_body(body: str) -> str:
    """Decode the interior of a git C-quoted token (no surrounding quotes)."""
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ord(ch) & 0xFF)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        nxt = body[i]
        if "0" <= nxt <= "7":
            j = i
            while j < n and j < i + 3 and "0" <= body[j] <= "7":
                j += 1
            out.append(int(body[i:j], 8) & 0xFF)
            i = j
            continue
        if nxt in _GIT_C_NAMED_ESCAPES:
            out.append(_GIT_C_NAMED_ESCAPES[nxt])
            i += 1
            continue
        out.append(ord(nxt) & 0xFF)
        i += 1
    # Invalid UTF-8 sequences become U+FFFD (parity with Node Buffer UTF-8 decode).
    # Do not use surrogateescape: companion dirty-guard paths must match.
    return out.decode("utf-8", errors="replace")


def decode_git_c_quoted_token(token: str) -> str:
    """Decode one git path token: C-quoted (``"..."`` with ``\\NNN``) or plain.

    Matches git core.quotePath / unquote_c_style. Do **not** apply to NUL-safe
    path_inventory / ``-z`` payloads (those are already raw).
    """
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return token
    return decode_git_c_escape_body(token[1:-1])


def parse_c_quoted_at(s: str, start: int) -> Tuple[Optional[str], int]:
    """Parse a C-quoted token starting at ``start`` (must be ``"``); return (decoded, next_i)."""
    if start >= len(s) or s[start] != '"':
        return None, start
    i = start + 1
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
            return decode_git_c_escape_body(s[start + 1 : i]), i + 1
        if ch != "\\":
            i += 1
            continue
        i += 1
        if i >= n:
            return None, start
        nxt = s[i]
        if "0" <= nxt <= "7":
            j = i
            while j < n and j < i + 3 and "0" <= s[j] <= "7":
                j += 1
            i = j
            continue
        # named or unknown single-char escape consumes one char
        i += 1
    return None, start


def strip_diff_git_ab_prefix(path: str) -> str:
    """Drop the ``a/`` or ``b/`` prefix git puts on ``diff --git`` path tokens."""
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def next_diff_git_token(s: str, i: int, *, is_first: bool) -> Tuple[Optional[str], int]:
    """Return the next ``diff --git`` path token (decoded, with ``a/``/``b/``) and new index."""
    n = len(s)
    while i < n and s[i] in " \t":
        i += 1
    if i >= n:
        return None, i
    if s[i] == '"':
        return parse_c_quoted_at(s, i)
    if is_first:
        # Unquoted first side: ends at `` b/`` (second side) or space before a quoted second.
        sep = s.find(" b/", i)
        if sep >= 0:
            return s[i:sep], sep
        sep = s.find(' "', i)
        if sep >= 0:
            return s[i:sep], sep
        return None, i
    return s[i:].rstrip(), n


def parse_diff_git_header_paths(rest: str) -> Optional[Tuple[str, str]]:
    """Parse the path pair after ``diff --git `` into repo-relative paths (no ``a/``/``b/``)."""
    a_raw, i = next_diff_git_token(rest, 0, is_first=True)
    if a_raw is None:
        return None
    b_raw, _ = next_diff_git_token(rest, i, is_first=False)
    if b_raw is None:
        return None
    return strip_diff_git_ab_prefix(a_raw), strip_diff_git_ab_prefix(b_raw)
