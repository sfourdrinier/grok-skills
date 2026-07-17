# wrapper/scripts/groklib/redaction.py
#
# Secret-material pattern table and redaction primitives (single source; C4
# envelope scanning and handoff blocker redaction both import from here).
# Extracted from envelope.py for the 900-line cap. The Node progress relay
# mirrors _SECRET_VALUE_PATTERNS under a drift test - update both together.

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr


def _log_stderr(function: str, message: str) -> None:
    log_stderr("redaction", function, message)


# Case-insensitive: a dict key CONTAINING any of these substrings, or ENDING
# with the singular "token" (or the plural "tokens", unless it is a benign
# usage-count field), is treated as secret-shaped regardless of its value.
# Substring matching catches composites like sessionToken, x-api-key, and
# clientSecret.
_SECRET_KEY_SUBSTRINGS = (
    "authorization",
    "cookie",
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
    "api_key",
    "api-key",
    "private_key",
    "privatekey",
    "signing_key",
    "signingkey",
    "ssh_key",
    "sshkey",
    "encryption_key",
    "encryptionkey",
)

# Plural "*tokens" keys that are benign token-COUNT usage fields (integer
# counts), NOT arrays of credential strings. A key ending in the plural
# "tokens" is secret-shaped (accessTokens, refreshTokens, sessionTokens,
# authTokens hold real token strings) EXCEPT when it ends with one of these
# usage-counter suffixes (inputTokens, outputTokens, totalTokens,
# cacheReadInputTokens, promptTokens, ...), which are never credentials. The
# singular "*token" is always secret-shaped: a usage counter is plural, never
# singular, so nothing benign ends in the singular "token".
_BENIGN_TOKEN_COUNT_KEY_SUFFIXES = (
    "inputtokens",
    "outputtokens",
    "totaltokens",
    "prompttokens",
    "completiontokens",
    "reasoningtokens",
    "cachedtokens",
    "cachetokens",
    "audiotokens",
    "acceptedpredictiontokens",
    "rejectedpredictiontokens",
)


def _key_segments(key: str) -> List[str]:
    """Split ``key`` into lowercased word segments across camelCase/snake/kebab/digit boundaries.

    So ``refreshTokenValue`` -> [refresh, token, value], ``refresh_token_value`` ->
    [refresh, token, value], and ``access-tokens`` -> [access, tokens]. Used to
    detect a secret-shaped word (``token``/``tokens``) that appears as an INTERIOR
    segment, not only as the trailing suffix (F2 secret-key-name-suffix: a
    ``refreshTokenValue`` holding a real OAuth refresh token was invisible because
    it ends in "value", not "token").

    The acronym-run boundary (``(?<=[A-Z])(?=[A-Z][a-z])``) is applied FIRST so a
    ``Token`` word glued directly onto an all-caps acronym is split out as its own
    segment: ``IDTokenHint`` -> [id, token, hint], ``JWTTokenPayload`` -> [jwt,
    token, payload] (F2 acronym-glued-key-name -- the lowercase->uppercase rule
    alone never fires between two uppercase letters, so the interior ``Token`` was
    glued into the acronym and missed). Benign counters stay excluded downstream by
    their normalized ``*tokens`` suffix, so this only ADDS detections.
    """
    spaced = re.sub(r"[^A-Za-z0-9]+", " ", key)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    spaced = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", spaced)
    return [segment.casefold() for segment in spaced.split() if segment]


def _is_secret_shaped_key(key: str) -> bool:
    lowered = key.casefold()
    if any(fragment in lowered for fragment in _SECRET_KEY_SUBSTRINGS):
        return True
    # Normalize away word separators so camelCase (inputTokens), snake_case
    # (input_tokens), and kebab-case (input-tokens) forms compare identically
    # against the token-count exclusion below.
    normalized = lowered.replace("_", "").replace("-", "")
    # A "token"/"tokens" WORD SEGMENT anywhere in the key (not only the trailing
    # suffix) is secret-shaped: sessionToken, refreshTokenValue, tokenSecretPair,
    # access_tokens all hold real credential strings. The ONLY exception is the
    # token-COUNT usage fields (inputTokens, outputTokens, totalTokens, ...), which
    # are benign integer counters -- excluded by their normalized suffix so a
    # counter is never redacted (F2 secret-key-name-suffix).
    segments = _key_segments(key)
    if "token" in segments or "tokens" in segments:
        return not any(normalized.endswith(suffix) for suffix in _BENIGN_TOKEN_COUNT_KEY_SUFFIXES)
    # Fallback for a glued lowercase key with no segment boundary (e.g.
    # "accesstoken"): the singular "*token" is always secret-shaped, and the
    # plural "*tokens" is secret-shaped unless it is a benign token-count field.
    if normalized.endswith("token"):
        return True
    if normalized.endswith("tokens"):
        return not any(normalized.endswith(suffix) for suffix in _BENIGN_TOKEN_COUNT_KEY_SUFFIXES)
    return False
# String VALUES anywhere in the structure that carry a raw credential shape are
# fail-closed too (SEC2), regardless of how benign the enclosing key looks (a
# token can leak into error.detail.stderr under a plain key). Each entry is
# (label, compiled pattern). Every pattern spans the ENTIRE secret (label PLUS
# the credential body) so that ``redact_secret_value_text`` -- which shares this
# exact table -- replaces the whole span and leaves NO credential text behind,
# and so ``assert_no_secret_material`` (which searches the same table) never
# flags a redacted string for a residual it failed to remove. The Node relay
# (plugin/scripts/progress-relay.mjs) hand-mirrors these exact pattern
# sources; a drift-guard test asserts the two byte-identical, so any change here
# MUST update that mirror in lock-step.
#   - bearer-token: the word "bearer" (case-insensitively, word-bounded),
#     whitespace, THEN a real credential body: a run of at least six token
#     characters that EITHER contains a digit / non-hyphen symbol OR is at least
#     twenty characters long. The first branch catches ordinary mixed tokens; the
#     second catches an all-letter/hyphen credential (round3
#     bearer-pattern-pure-alpha-token-false-negative) while a 20-char floor keeps
#     ordinary English words after "bearer" (e.g. "Bearer authentication") from
#     matching. The token body is captured in full so redaction removes the value.
#   - api-key-token: one of the real provider key shapes, each anchored at a
#     word boundary and each requiring a long HIGH-ENTROPY body containing a
#     digit so ordinary kebab-case identifiers are never matched:
#       * xai-<>=20 [A-Za-z0-9_] with a digit  -- xAI keys (no hyphen in body).
#       * sk-proj-<>=40 [A-Za-z0-9_-] with a digit -- current OpenAI project keys
#         (sk-proj-...): the body is base64url so hyphens/underscores are allowed,
#         but the 40-char + must-contain-a-digit floor excludes kebab branch names.
#       * sk-ant-<seg>-<>=40 [A-Za-z0-9_-] with a digit -- Anthropic keys
#         (sk-ant-api03-..., sk-ant-admin01-...): the "<seg>" matches api03/admin01.
#       * sk-<>=20 [A-Za-z0-9_] with a digit (NO hyphen) -- legacy OpenAI keys.
#     The word boundary prevents ordinary prose like "task-force"/"risk-averse"
#     from matching the "sk-" fragment; the no-hyphen (legacy/xai) OR the
#     40-char-with-digit (sk-proj/sk-ant) requirement stops kebab identifiers such
#     as "sk-learn-preprocessing-utils" or "feature/sk-image-augmentation-pipeline"
#     from being redacted, while every real current provider key still matches
#     (round6 api-key-under-match: the round5 no-hyphen body missed sk-proj-/
#     sk-ant-api03- entirely because the hyphen 3-5 chars in broke the run).
#   - jwt: a base64url JWT header (eyJ...) followed by two more dot-separated
#     base64url segments. eyJ is the exact base64 of `{"`, so a JWT header
#     always starts with it; the two trailing segments avoid matching an
#     ordinary word that merely begins "eyJ".
#   - aws-access-key-id: the AKIA (long-term IAM user) or ASIA (STS temporary /
#     session, the default in CI/CD, Lambda, and assumed-role workflows) prefix
#     plus the fixed 16 upper/-digit body (round6 aws-ASIA).
#   - github-token: a gh[posru]_ classic personal/OAuth/refresh/user/server token
#     prefix OR the github_pat_ fine-grained personal-access-token prefix (the
#     currently recommended format), plus a long token body (underscores allowed
#     so a fine-grained PAT's second segment is captured, not just its prefix).
#   - slack-token: an xox[baprs]- bot/app/refresh/personal/... token prefix.
#   - pem-private-key: a PEM/PGP private-key block, matched from the BEGIN marker
#     through its -----END ... PRIVATE KEY[ BLOCK]----- line when present, or to
#     end-of-string when NO end marker is present -- so the base64 key body is
#     ALWAYS removed by redaction, never left behind when the block is truncated
#     or its closing line was cut off. The trailing "$" anchor (end-of-input, no
#     MULTILINE) is byte-identical in Python and the Node relay mirror. The
#     optional " BLOCK" token also matches the PGP armored header
#     "-----BEGIN PGP PRIVATE KEY BLOCK-----" (F5), alongside the RSA/EC/OPENSSH
#     PEM variants.
_SECRET_VALUE_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("bearer-token", re.compile(r"(?i)\bbearer\s+(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=]|[A-Za-z0-9._~+/=-]{20,})[A-Za-z0-9._~+/=-]{6,}")),
    ("api-key-token", re.compile(r"\b(?:xai-(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9_]{20,}|sk-proj-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{40,}|sk-ant-[a-z0-9]+-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{40,}|sk-(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9_]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,})")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[posru]_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|$)")),
)


class SecretMaterialError(GrokWrapperError):
    """Raised by assert_no_secret_material when a secret-shaped key or bearer-token-shaped value is found.

    Classified as C4 error class "validation-failure": this is the last
    line of defense before envelope content reaches stdout, the stored
    envelope file, or any other output surface.
    """

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("validation-failure", message, detail)



def _is_secret_sequence(obj: object) -> bool:
    """True for a JSON-shaped sequence container (list OR tuple), never a str/bytes.

    json.dumps silently serializes a tuple as a JSON array, so a secret nested
    inside a tuple reaches stdout/disk exactly like one nested inside a list; the
    scanner and the redactor must therefore recurse into BOTH (round3
    envelope-tuple-secret-scan-gap). str/bytes are explicitly excluded: they are
    scalar leaves, not containers to iterate character-by-character.
    """
    return isinstance(obj, (list, tuple)) and not isinstance(obj, (str, bytes, bytearray))


def _walk_secret_tree(obj: object, on_key, on_string, path: str = "$") -> object:
    """Single recursive walker over a JSON-shaped value, shared by scan and redaction.

    Recurses into dict values, every non-string/bytes sequence (list AND tuple),
    and string leaves. ``on_key(path, key)`` is invoked for every string dict
    key; ``on_string(path, s)`` is invoked for every string leaf and its return
    value replaces that leaf. Returns a rebuilt structure (dicts and sequences
    rebuilt; tuples become lists so the result is JSON-shaped). Both
    ``assert_no_secret_material`` (whose callbacks raise on a secret and return
    the string unchanged) and ``redact_secret_material`` (whose string callback
    returns the redacted text) drive this one walker, so the recursion shape can
    never drift between the scan and the redaction -- which is exactly why the
    tuple gap previously existed in two places at once.

    ``on_key`` may return None (keep the key and recurse into its value) OR a
    ``(replacement_key, suppressed_value)`` tuple (the failure-detail key
    redactor's signal for a secret-shaped key: the key is renamed to a safe
    placeholder and its value replaced wholesale, since a secret-shaped key
    implies a secret value the value-pattern redactor might not recognize). The
    scanner's and the default redactor's ``on_key`` both return None, so this
    key-rewrite branch is inert for them.
    """
    if isinstance(obj, dict):
        rebuilt: Dict[object, object] = {}
        for key, nested_value in obj.items():
            key_action = on_key(path, key) if isinstance(key, str) else None
            if key_action is not None:
                replacement_key, suppressed_value = key_action
                rebuilt[replacement_key] = suppressed_value
            else:
                rebuilt[key] = _walk_secret_tree(nested_value, on_key, on_string, "{}.{}".format(path, key))
        return rebuilt
    if _is_secret_sequence(obj):
        return [
            _walk_secret_tree(item, on_key, on_string, "{}[{}]".format(path, index))
            for index, item in enumerate(obj)
        ]
    if isinstance(obj, str):
        return on_string(path, obj)
    return obj


def assert_no_secret_material(obj: object, _path: str = "$") -> None:
    """Recursively walk a JSON-shaped value and raise SecretMaterialError on any secret-shaped content.

    Raises when a dict key is secret-shaped (case-insensitively CONTAINS
    authorization, cookie, secret, password, passwd, credential, apikey,
    api_key, or api-key, or ENDS with the singular "token"), or when a
    string value anywhere in the structure matches one of the raw-credential
    value shapes in ``_SECRET_VALUE_PATTERNS`` (a bearer token, an xai-/sk-
    prefixed API key, a JWT, an AWS access-key id, a GitHub/Slack token, or a
    PEM private-key block). Recurses through dict values and EVERY sequence
    container (list AND tuple), so a secret nested inside a tuple -- which
    json.dumps would serialize verbatim as a JSON array -- is caught too. Fail
    closed: this is the last check ``build_envelope`` runs before handing back a
    candidate envelope for stdout/disk emission.
    """

    def _check_key(key_path: str, key: str) -> None:
        if _is_secret_shaped_key(key):
            _log_stderr("assert_no_secret_material", "secret-shaped key found at {}.{}".format(key_path, key))
            raise SecretMaterialError(
                "secret-shaped key {!r} found at {}".format(key, key_path),
                {"path": key_path, "key": key},
            )

    def _check_string(string_path: str, value: str) -> str:
        for label, pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                _log_stderr("assert_no_secret_material", "{} value found at {}".format(label, string_path))
                raise SecretMaterialError(
                    "value at {} matches the {} pattern".format(string_path, label),
                    {"path": string_path, "pattern": label},
                )
        return value

    _walk_secret_tree(obj, _check_key, _check_string, _path)


def redact_secret_value_text(text: str) -> str:
    """Replace every secret-shaped substring in ``text`` with a labeled placeholder.

    Uses the SAME ``_SECRET_VALUE_PATTERNS`` that ``assert_no_secret_material``
    flags (single source of truth: the patterns are never duplicated), so any
    string this returns is guaranteed to pass the scanner's value checks. Each
    pattern's matches are replaced with ``[redacted-<label>]``; the placeholders
    themselves match no pattern, and every pattern is applied in turn so a value
    that reveals a second secret shape after the first is masked (e.g. a JWT that
    only becomes visible once the leading ``bearer `` is stripped) is caught too.
    """
    redacted = text
    for label, pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[redacted-{}]".format(label), redacted)
    return redacted


_REDACTED_STREAM_PLACEHOLDER = "[redacted-secret]"


def _apply_mask_slice(text: str, masked: bytearray, start: int, end: int) -> str:
    """Rebuild ``text[start:end]``, collapsing each maximal masked run into one placeholder."""
    pieces: List[str] = []
    index = start
    while index < end:
        if masked[index]:
            pieces.append(_REDACTED_STREAM_PLACEHOLDER)
            while index < end and masked[index]:
                index += 1
        else:
            run_start = index
            while index < end and not masked[index]:
                index += 1
            pieces.append(text[run_start:index])
    return "".join(pieces)


def redact_secret_text_stream(segments: List[str]) -> List[str]:
    """Redact secret spans across the CONCATENATION of ``segments`` (a chunked text stream).

    The segments are treated as ONE continuous text -- exactly as a streaming
    coalescer concatenates its ~480-char chunks -- so a secret split across a
    segment boundary is caught even though neither half matches on its own
    (Grok dogfood-3 #6: per-event redaction lets a bearer/PEM split across two
    progress events slip through). Returns a NEW list of the same length; each
    returned segment is its slice of the redacted concatenation, with any secret
    span it overlaps (wholly or partly) replaced by a placeholder. Shares the
    single ``_SECRET_VALUE_PATTERNS`` source with the scanner and the per-value
    redactor, so the three can never diverge.
    """
    joined = "".join(segments)
    if not joined:
        return list(segments)
    masked = bytearray(len(joined))
    for _label, pattern in _SECRET_VALUE_PATTERNS:
        for match in pattern.finditer(joined):
            for index in range(match.start(), match.end()):
                masked[index] = 1
    if not any(masked):
        return list(segments)
    result: List[str] = []
    cursor = 0
    for segment in segments:
        segment_end = cursor + len(segment)
        result.append(_apply_mask_slice(joined, masked, cursor, segment_end))
        cursor = segment_end
    return result


def redact_secret_material(obj: object, *, redact_keys: bool = False) -> object:
    """Return a deep copy of a JSON-shaped value with secret-shaped string values redacted.

    Every string value anywhere in the structure is passed through
    ``redact_secret_value_text``; dicts and lists are rebuilt recursively.

    With ``redact_keys=False`` (the default) dict KEYS are left untouched: this
    mode embeds wrapper-generated event text (whose keys are never secret-shaped)
    into an envelope safely, NOT to relax ``assert_no_secret_material``'s key
    rule. The scanner still runs afterwards inside ``build_envelope`` as the
    fail-closed last line of defense.

    With ``redact_keys=True`` a secret-shaped KEY (per ``_is_secret_shaped_key``)
    is renamed to a collision-free ``[redacted-key-N]`` placeholder and its whole
    value is replaced with the redaction placeholder -- consistently with what the
    scanner would flag (round6 failure-detail-key-redaction: a caller-supplied
    ``error.detail`` whose key merely CONTAINS a secret-shaped substring, e.g.
    ``credentialDelegation``, must not hard-fail terminalization; and a real
    opaque secret held under a secret-shaped key is suppressed, not just
    value-pattern redacted). Used by ``failure_envelope`` for ``error.detail``.

    Shares ``_walk_secret_tree`` with the scanner (DRY), so it recurses through
    the same containers -- dict values plus every sequence including tuples --
    and a secret nested inside a tuple is masked, not left intact.
    """
    placeholder_counter: Dict[str, int] = {"n": 0}

    def _key_action(_key_path: str, key: str):
        if not redact_keys or not _is_secret_shaped_key(key):
            return None
        index = placeholder_counter["n"]
        placeholder_counter["n"] += 1
        return ("[redacted-key-{}]".format(index), _REDACTED_STREAM_PLACEHOLDER)

    def _redact_string(_string_path: str, value: str) -> str:
        return redact_secret_value_text(value)

    return _walk_secret_tree(obj, _key_action, _redact_string)


