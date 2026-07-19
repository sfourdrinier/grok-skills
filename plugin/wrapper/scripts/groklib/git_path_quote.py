# wrapper/scripts/groklib/git_path_quote.py
#
# Git core.quotePath C-style path decoding for non -z outputs (diff --git headers,
# apply --numstat, etc.). Distinct from path_inventory: NUL-safe -z inventories
# are already raw and must never be C-unquoted. Shared golden vectors:
# plugin/references/git-c-quoted-path-vectors.json (parity with Node unquoteGitPath).
# Invalid UTF-8 after decode uses U+FFFD (errors="replace") to match Node Buffer.

from __future__ import annotations

from typing import List, Optional, Tuple

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


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t":
        i += 1
    return i


def _unquoted_split_candidates(rest: str) -> List[Tuple[str, str]]:
    """All reconstructible ``a/...`` / ``b/...`` splits on a fully unquoted rest.

    Git's grammar is ``diff --git <a-path> <b-path>`` where each side is either
    C-quoted or a raw path that may contain spaces and even the substring `` b/``.
    When both sides are unquoted the separator is ambiguous, so enumerate every
    `` b/`` occurrence that yields ``a/...`` left and ``b/...`` right.
    """
    out: List[Tuple[str, str]] = []
    start = 0
    while True:
        sep = rest.find(" b/", start)
        if sep < 0:
            break
        left = rest[:sep]
        right = rest[sep + 1 :]  # keep leading ``b/``
        if left.startswith("a/") and right.startswith("b/"):
            out.append((left, right))
        start = sep + 1
    return out


def _choose_unquoted_pair(rest: str) -> Optional[Tuple[str, str]]:
    """Pick a unique dual-condition unquoted path pair, else fail closed.

    Preference order (fail closed on residual ambiguity):
    1. Exactly one candidate where strip(a) == strip(b) (same-path edit/add/delete).
    2. Exactly one reconstructible candidate overall (ordinary rename / unique split).
    3. Otherwise None (e.g. rename with `` b/`` in both sides).
    """
    candidates = _unquoted_split_candidates(rest)
    if not candidates:
        return None
    equal = [
        (a, b)
        for a, b in candidates
        if strip_diff_git_ab_prefix(a) == strip_diff_git_ab_prefix(b)
    ]
    if len(equal) == 1:
        return equal[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def next_diff_git_token(s: str, i: int, *, is_first: bool) -> Tuple[Optional[str], int]:
    """Return the next ``diff --git`` path token (decoded, with ``a/``/``b/``) and new index.

    Used when at least one side is C-quoted (quoted tokens have unambiguous bounds).
    Fully unquoted headers go through ``_choose_unquoted_pair`` instead.
    """
    n = len(s)
    i = _skip_ws(s, i)
    if i >= n:
        return None, i
    if s[i] == '"':
        return parse_c_quoted_at(s, i)
    if is_first:
        # Unquoted first side ends at the space before a quoted second side, or
        # (fallback) at `` b/`` when the second side is also unquoted and unique.
        qsep = s.find(' "', i)
        if qsep >= 0:
            return s[i:qsep], qsep
        pair = _choose_unquoted_pair(s[i:])
        if pair is None:
            return None, i
        a_raw, _ = pair
        # Advance past a_raw and the separating space so the second call sees b/...
        return a_raw, i + len(a_raw)
    return s[i:].rstrip(), n


def parse_diff_git_header_paths(rest: str) -> Optional[Tuple[str, str]]:
    """Parse the path pair after ``diff --git `` into repo-relative paths (no ``a/``/``b/``).

    State machine:
    - Leading whitespace skipped.
    - If the first non-space char is ``"``, parse C-quoted a-side then b-side.
    - If both sides are unquoted, use dual-condition balanced separator selection
      (equal stripped paths preferred; unique candidate otherwise; else None).
    - Malformed / ambiguous input returns None (fail closed).
    """
    if not rest or not rest.strip():
        return None
    i = _skip_ws(rest, 0)
    if i >= len(rest):
        return None

    # Fully unquoted rest: dual-condition selector is the SSOT.
    if rest[i] != '"':
        # Mixed: unquoted first, quoted second -> next_diff_git_token path.
        if ' "' in rest[i:]:
            a_raw, j = next_diff_git_token(rest, i, is_first=True)
            if a_raw is None:
                return None
            b_raw, _ = next_diff_git_token(rest, j, is_first=False)
            if b_raw is None:
                return None
            return strip_diff_git_ab_prefix(a_raw), strip_diff_git_ab_prefix(b_raw)
        pair = _choose_unquoted_pair(rest[i:])
        if pair is None:
            return None
        a_raw, b_raw = pair
        return strip_diff_git_ab_prefix(a_raw), strip_diff_git_ab_prefix(b_raw)

    a_raw, j = next_diff_git_token(rest, i, is_first=True)
    if a_raw is None:
        return None
    b_raw, _ = next_diff_git_token(rest, j, is_first=False)
    if b_raw is None:
        return None
    return strip_diff_git_ab_prefix(a_raw), strip_diff_git_ab_prefix(b_raw)
