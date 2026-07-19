# wrapper/scripts/groklib/injectedsecrets.py
#
# D4(a) exact-value injected-credential redaction. The wrapper copies the
# operator's real auth material (auth.json) into every per-run private Grok
# HOME, so the wrapper KNOWS the exact secret value(s) it injected. This
# module captures those exact values into a per-run denylist and redacts ANY
# exact occurrence of them from the C4 stdout envelope by EXACT MATCH,
# regardless of the value's shape. That closes the case where Grok reads and
# echoes its own injected auth token in a form the pattern scanner
# (envelope.assert_no_secret_material / _SECRET_VALUE_PATTERNS) cannot
# recognize (e.g. a 40-char opaque token under an unusual key). This runs IN
# ADDITION to the pattern scanner, never instead of it: build_envelope applies
# this exact-value redaction first, then the pattern scanner still runs as the
# fail-closed last line of defense.
#
# Fail-safe by construction: extraction never raises. If the copied auth.json
# cannot be read or parsed, extraction logs to stderr and yields an EMPTY
# denylist, so the run continues (the pattern scanner still applies) and the
# private home is never left half-built by a redaction concern.
#
# Secrets discipline: the captured values live ONLY in this module's private
# per-run global. They are never logged, never returned to a caller, and never
# embedded in an exception message. The single stderr log this module writes on
# a read/parse failure names the failure and the file path only, never any
# parsed value.

import json
import pathlib
from typing import List, Tuple  # List used by _mask_text rebuild

from groklib import log_stderr

# The exact substring every injected-credential occurrence is replaced with.
# Two hard constraints, both required so a masked envelope passes
# envelope.assert_no_secret_material cleanly rather than tripping it:
#   1. It must match NONE of envelope._SECRET_VALUE_PATTERNS (so it is safe in a
#      string VALUE position) -- no bearer/xai-/sk-/eyJ/AKIA/gh_/xox/PEM shape.
#   2. It must contain NONE of envelope._SECRET_KEY_SUBSTRINGS (authorization,
#      cookie, secret, password, passwd, credential, apikey, api_key, api-key)
#      and must not end in "token"/"tokens" (so it is safe in a dict KEY
#      position, since an injected value can be echoed as a structured key). This
#      is why the word "credential" itself is deliberately NOT in the placeholder.
INJECTED_CREDENTIAL_PLACEHOLDER = "[redacted-injected-value]"

# A copied auth.json string leaf value is treated as an injected credential
# only when it is at least this long. The wrapper does NOT depend on the key
# name (robust against renamed/nested credential fields); a 16-char floor keeps
# short, benign scalar strings (flags, versions, short ids) out of the denylist
# while still capturing every realistic opaque token / key / cookie.
_MIN_CREDENTIAL_VALUE_LENGTH = 16

# The per-run injected-credential denylist. Each wrapper invocation is a single
# process handling a single mode run, so a module-level per-run value is the
# correct scope: it is set once when the private home is created and read by
# envelope.build_envelope for every envelope the run emits (success, failure,
# terminal), which are all built AFTER the private home is torn down. Sorted by
# descending length so a longer secret is masked before any shorter secret that
# might be its substring, leaving no residual.
_INJECTED_SECRET_DENYLIST: Tuple[str, ...] = ()


def _log(function: str, message: str) -> None:
    log_stderr("injectedsecrets", function, message)


def set_injected_secret_denylist(values: "object") -> None:
    """Replace the per-run injected-credential denylist with the exact ``values``.

    Accepts any iterable of strings; non-string and empty entries are dropped,
    duplicates are collapsed, and the result is stored sorted by descending
    length (then lexicographically for determinism) so the redactor masks a
    longer secret before any shorter secret that could be its substring. This
    REPLACES the denylist wholesale (never accumulates across homes), so a
    second private home created in the same process reflects only its own
    injected values.
    """
    global _INJECTED_SECRET_DENYLIST
    unique: List[str] = []
    seen = set()
    if isinstance(values, (list, tuple)):
        candidates = values
    else:
        candidates = list(values) if values is not None else []
    for value in candidates:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    unique.sort(key=lambda item: (-len(item), item))
    _INJECTED_SECRET_DENYLIST = tuple(unique)


def clear_injected_secret_denylist() -> None:
    """Empty the per-run injected-credential denylist (used at teardown and in tests)."""
    global _INJECTED_SECRET_DENYLIST
    _INJECTED_SECRET_DENYLIST = ()


def current_injected_secret_denylist() -> Tuple[str, ...]:
    """Return the current per-run injected-credential denylist (read-only, for tests/inspection)."""
    return _INJECTED_SECRET_DENYLIST


def _collect_string_leaves(value: object, out: List[str]) -> None:
    """Recursively collect every string leaf VALUE at or under ``value`` into ``out``.

    Dict VALUES, list/tuple items, and bare string leaves are collected; dict
    KEYS are field names, not credentials, so they are never collected. Only
    values at least ``_MIN_CREDENTIAL_VALUE_LENGTH`` characters long are kept.
    """
    if isinstance(value, str):
        if len(value) >= _MIN_CREDENTIAL_VALUE_LENGTH:
            out.append(value)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_string_leaves(nested, out)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_string_leaves(item, out)


def extract_injected_secret_values(grok_dir: pathlib.Path, auth_file_names: "Tuple[str, ...]") -> Tuple[str, ...]:
    """Extract the injected-credential denylist from the COPIED auth files under ``grok_dir``.

    For each name in ``auth_file_names`` present as a regular file directly
    under ``grok_dir`` (the private home's ``.grok`` copy), the file is parsed
    as JSON and every string leaf value of length >= 16 is captured, regardless
    of key name (robust to renamed/nested credential fields). Fail-safe: a
    missing/unreadable/malformed file is logged to stderr and skipped, yielding
    no values for that file rather than raising. Never raises; on total failure
    returns an empty tuple so the caller degrades to the pattern scanner alone.
    """
    collected: List[str] = []
    for name in auth_file_names:
        auth_path = grok_dir / name
        try:
            raw_text = auth_path.read_text(encoding="utf-8")
        except OSError as exc:
            _log("extract_injected_secret_values", "could not read copied auth file {}: {}".format(auth_path, exc))
            continue
        except Exception as exc:  # decode or unexpected read error: fail safe, never crash the run
            _log("extract_injected_secret_values", "unexpected error reading {}: {}".format(auth_path, exc))
            continue
        try:
            parsed = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            _log(
                "extract_injected_secret_values",
                "copied auth file {} is not valid JSON; no exact-value denylist from it: {}".format(auth_path, exc),
            )
            continue
        _collect_string_leaves(parsed, collected)

    unique: List[str] = []
    seen = set()
    for value in collected:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    unique.sort(key=lambda item: (-len(item), item))
    return tuple(unique)


def register_injected_secrets_from_home(grok_dir: pathlib.Path, auth_file_names: "Tuple[str, ...]") -> None:
    """Extract injected credentials from a private home and register them.

    Called by authhome.create_private_home after auth material is copied, and by
    peer-stop finalize reload. Extraction never raises.

    Trustworthy (non-empty) extracted values always replace the denylist.
    When extraction yields nothing (missing/unreadable/malformed home), preserve
    any existing non-empty denylist so resident finalize cannot wipe a valid
    in-memory set after auth is gone. Local new-process with empty denylist and
    missing home stays pattern-only.
    """
    values = extract_injected_secret_values(grok_dir, auth_file_names)
    if values:
        set_injected_secret_denylist(values)
        return
    if current_injected_secret_denylist():
        _log(
            "register_injected_secrets_from_home",
            "extraction empty for {}; preserving existing non-empty denylist".format(
                grok_dir
            ),
        )
        return
    set_injected_secret_denylist(())


def _mask_text(text: str, denylist: Tuple[str, ...]) -> str:
    """Replace every denylisted value occurrence (exact + casefold) with the placeholder."""
    if not denylist or not text:
        return text
    # Longest-first so overlapping secrets mask the longest form first.
    ordered = sorted(denylist, key=lambda item: (-len(item), item))
    masked = text
    lower_masked = masked.casefold()
    for secret in ordered:
        if not secret:
            continue
        # Exact first
        if secret in masked:
            masked = masked.replace(secret, INJECTED_CREDENTIAL_PLACEHOLDER)
            lower_masked = masked.casefold()
            continue
        # Case-insensitive scan without regex (secrets may contain special chars)
        needle = secret.casefold()
        if not needle:
            continue
        start = 0
        pieces: List[str] = []
        cursor = 0
        while True:
            idx = lower_masked.find(needle, start)
            if idx < 0:
                break
            pieces.append(masked[cursor:idx])
            pieces.append(INJECTED_CREDENTIAL_PLACEHOLDER)
            cursor = idx + len(secret)
            start = cursor
        if pieces:
            pieces.append(masked[cursor:])
            masked = "".join(pieces)
            lower_masked = masked.casefold()
    return masked


def redact_injected_secrets(obj: object) -> object:
    """Return a deep copy of a JSON-shaped value with every exact injected-credential occurrence masked.

    Both dict KEYS and every string leaf VALUE anywhere in the structure have
    each exact denylisted value substring replaced with
    ``INJECTED_CREDENTIAL_PLACEHOLDER`` (keys are masked too because Grok's
    structured output can place an echoed token in a dict key). Lists and
    tuples are rebuilt (tuples become lists so the result is JSON-shaped). When
    the denylist is empty the input is returned unchanged, so this is a no-op
    for any run that captured no injected credentials.
    """
    denylist = _INJECTED_SECRET_DENYLIST
    if not denylist:
        return obj
    if isinstance(obj, str):
        return _mask_text(obj, denylist)
    if isinstance(obj, dict):
        rebuilt = {}
        for key, value in obj.items():
            masked_key = _mask_text(key, denylist) if isinstance(key, str) else key
            rebuilt[masked_key] = redact_injected_secrets(value)
        return rebuilt
    if isinstance(obj, (list, tuple)):
        return [redact_injected_secrets(item) for item in obj]
    return obj
