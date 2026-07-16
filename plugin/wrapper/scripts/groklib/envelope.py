# wrapper/scripts/groklib/envelope.py
#
# C4 result envelope: builds, structurally validates, and emits the single
# JSON document every wrapper subcommand prints to stdout (and stores at
# runs/<run-id>/envelope.json). Validation is hand-rolled structural
# checking against the declarative FIELD_SPECS table below, deliberately
# with no external jsonschema dependency (stdlib only, per Global
# Constraints).

import json
import os
import pathlib
import re
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import injectedsecrets
from groklib import platformsupport

SCHEMA_VERSION = 1

MODES: Tuple[str, ...] = (
    "preflight",
    "review",
    "reason",
    "code",
    "verify",
    "status",
    "cleanup",
    "handoff",
)

# "running" is for status-mode inspection of an in-progress target run (no
# stored envelope yet). Other modes use only success/failure.
STATUSES: Tuple[str, ...] = ("success", "failure", "running")

CLEANUP_STATUSES: Tuple[str, ...] = ("clean", "retained", "failed", "not-applicable")

# Exact C4 error-class registry. Order matches the plan's normative list.
ERROR_CLASSES: Tuple[str, ...] = (
    "auth-missing",
    "version-mismatch",
    "model-unavailable",
    "invalid-target",
    "rules-parity-failure",
    "worktree-failure",
    "sandbox-failure",
    "wrong-working-directory",
    "tool-unavailable",
    "verifier-unavailable",
    "output-missing",
    "output-malformed",
    "schema-mismatch",
    "timeout",
    "turn-exhaustion",
    "cancelled",
    "cli-failure",
    "unexpected-edits",
    "validation-failure",
    "cleanup-failure",
    "state-ownership-violation",
    "leader-socket-failure",
    "usage-error",
    "probe-required",
    "finalization-timeout",
    "finalization-worker-missing-result",
    "finalization-worker-unkillable",
    "isolation-unavailable",
    # PR4 implementation handoff (exactly seven new classes)
    "implementation-contract-invalid",
    "write-scope-violation",
    "unexpected-commit",
    "artifact-generation-failure",
    "artifact-integrity-failure",
    "handoff-unavailable",
    "terminal-envelope-incomplete",
)

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

_STORED_ENVELOPE_FILE_MODE = 0o600


def _log_stderr(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "envelope" component."""
    log_stderr("envelope", function, message)


class InvalidEnvelopeError(ValueError):
    """Raised when a build_envelope/failure_envelope call violates the C4 field contract.

    A ``ValueError``, not a ``GrokWrapperError``: mirrors
    ``groklib.progress.InvalidProgressEventError``. This guards the C4
    schema contract against a programmer error in the mode handlers that
    call ``build_envelope`` (an unknown field name, an unregistered error
    class, a structurally malformed value), not an operator-facing
    classified failure the wrapper would report as an "error" envelope.
    """

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__(message)
        self.detail: Dict[str, object] = detail if detail is not None else {}


class SecretMaterialError(GrokWrapperError):
    """Raised by assert_no_secret_material when a secret-shaped key or bearer-token-shaped value is found.

    Classified as C4 error class "validation-failure": this is the last
    line of defense before envelope content reaches stdout, the stored
    envelope file, or any other output surface.
    """

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("validation-failure", message, detail)


# ---------------------------------------------------------------------------
# Structural type-spec vocabulary used by FIELD_SPECS and _validate_value.
#
# Each type spec is a tuple whose first element names the kind and whose
# remaining elements carry kind-specific parameters (an enum's allowed
# values, an object's field-shape table, an array's item shape). This is
# the entire "declarative FIELD_SPECS table" the brief asks for: adding a
# new C4 field means adding one FIELD_SPECS entry, not writing bespoke
# validation code.
# ---------------------------------------------------------------------------

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_object(
    value: object,
    shape: Dict[str, tuple],
    path: str,
    violations: List[str],
    *,
    optional_shape: Optional[Dict[str, tuple]] = None,
) -> None:
    """Validate ``value`` is a dict matching ``shape``: every required key present.

    When ``optional_shape`` is set, those keys may be omitted (backward-compatible
    command evidence). If present, they must match their type_spec. Unknown keys
    outside required+optional still fail.
    """
    if not isinstance(value, dict):
        violations.append("{}: expected object, got {}".format(path, type(value).__name__))
        return

    optional_shape = optional_shape or {}
    for field_name, type_spec in shape.items():
        field_path = "{}.{}".format(path, field_name)
        if field_name not in value:
            violations.append("{}: missing field".format(field_path))
            continue
        _validate_value(value[field_name], type_spec, field_path, violations)

    for field_name, type_spec in optional_shape.items():
        if field_name not in value:
            continue
        field_path = "{}.{}".format(path, field_name)
        _validate_value(value[field_name], type_spec, field_path, violations)

    allowed = set(shape.keys()) | set(optional_shape.keys())
    unknown_fields = set(value.keys()) - allowed
    for field_name in sorted(unknown_fields):
        violations.append("{}.{}: unknown field".format(path, field_name))


def _safe_value_placeholder(value: object) -> str:
    """A value-free (or redacted) placeholder for an offending value in a violation string.

    Structural-validation violations are logged to stderr AND embedded in
    InvalidEnvelopeError.detail BEFORE assert_no_secret_material ever runs, so a
    raw ``{!r}`` of the value would leak a secret-shaped enum/const value (F1)
    ahead of the scanner. Mirroring the scanner callbacks' value-free discipline:
    a string value is passed through ``redact_secret_value_text`` (so any known
    credential shape is masked), and a non-string value is reduced to a bare type
    placeholder that carries no content at all.
    """
    if isinstance(value, str):
        return "'{}'".format(redact_secret_value_text(value))
    return "<{}>".format(type(value).__name__)


def _validate_value(value: object, type_spec: tuple, path: str, violations: List[str]) -> None:
    kind = type_spec[0]

    if kind == "const_int":
        expected = type_spec[1]
        if value != expected or isinstance(value, bool):
            violations.append(
                "{}: expected constant {!r}, got {}".format(path, expected, _safe_value_placeholder(value))
            )
    elif kind == "str":
        if not isinstance(value, str):
            violations.append("{}: expected string, got {}".format(path, type(value).__name__))
    elif kind == "str_or_null":
        if value is not None and not isinstance(value, str):
            violations.append("{}: expected string or null, got {}".format(path, type(value).__name__))
    elif kind == "int":
        if not _is_int(value):
            violations.append("{}: expected int, got {}".format(path, type(value).__name__))
    elif kind == "int_or_null":
        if value is not None and not _is_int(value):
            violations.append("{}: expected int or null, got {}".format(path, type(value).__name__))
    elif kind == "number":
        if not _is_number(value):
            violations.append("{}: expected number, got {}".format(path, type(value).__name__))
    elif kind == "bool":
        if not isinstance(value, bool):
            violations.append("{}: expected bool, got {}".format(path, type(value).__name__))
    elif kind == "bool_or_null":
        if value is not None and not isinstance(value, bool):
            violations.append("{}: expected bool or null, got {}".format(path, type(value).__name__))
    elif kind == "object_or_null":
        if value is not None and not isinstance(value, dict):
            violations.append("{}: expected object or null, got {}".format(path, type(value).__name__))
    elif kind == "object_or_str_or_null":
        if value is not None and not isinstance(value, (dict, str)):
            violations.append("{}: expected object, string, or null, got {}".format(path, type(value).__name__))
    elif kind == "enum":
        allowed_values = type_spec[1]
        if value not in allowed_values:
            violations.append(
                "{}: expected one of {}, got {}".format(path, allowed_values, _safe_value_placeholder(value))
            )
    elif kind == "array_of_str":
        if not isinstance(value, list):
            violations.append("{}: expected array, got {}".format(path, type(value).__name__))
        else:
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    violations.append("{}[{}]: expected string, got {}".format(path, index, type(item).__name__))
    elif kind == "array_of_object":
        item_shape = type_spec[1]
        optional_item = type_spec[2] if len(type_spec) > 2 else None
        if not isinstance(value, list):
            violations.append("{}: expected array, got {}".format(path, type(value).__name__))
        else:
            for index, item in enumerate(value):
                _validate_object(
                    item,
                    item_shape,
                    "{}[{}]".format(path, index),
                    violations,
                    optional_shape=optional_item,
                )
    elif kind == "object":
        shape = type_spec[1]
        _validate_object(value, shape, path, violations)
    else:
        raise AssertionError("unknown type spec kind {!r} at {}".format(kind, path))


# ---------------------------------------------------------------------------
# Nested-object shapes for the compound C4 fields.
# ---------------------------------------------------------------------------

_SANDBOX_SHAPE: Dict[str, tuple] = {
    "requestedProfile": ("str_or_null",),
    "reportedProfile": ("str_or_null",),
    "enforced": ("bool_or_null",),
    "evidence": ("str_or_null",),
}

_POLICY_SHAPE: Dict[str, tuple] = {
    "tools": ("array_of_str",),
    "permissionMode": ("str_or_null",),
    "subagents": ("bool",),
    "webAccess": ("bool",),
    "memory": ("bool",),
}

_GROK_SHAPE: Dict[str, tuple] = {
    "sessionId": ("str_or_null",),
    "requestId": ("str_or_null",),
    "stopReason": ("str_or_null",),
    "modelUsage": ("object_or_null",),
}

_USAGE_SHAPE: Dict[str, tuple] = {
    "turns": ("int_or_null",),
    "raw": ("object_or_null",),
}

_INSTRUCTION_ITEM_SHAPE: Dict[str, tuple] = {
    "path": ("str",),
    "bytes": ("int",),
    "sha256": ("str",),
}

_COMMAND_TAIL_SHAPE: Dict[str, tuple] = {
    "text": ("str",),
    "truncated": ("bool",),
    "bytes": ("int",),
}

# Required fields for every commands[] entry (pre-1.6.0 and 1.6.0+).
_COMMAND_ITEM_SHAPE: Dict[str, tuple] = {
    "argv": ("array_of_str",),
    "exitStatus": ("int",),
    "durationSeconds": ("number",),
    "purpose": ("str",),
    "cwd": ("str",),  # command's working dir: makes the build gate's location pinning auditable
}

# Optional PR4 §14.13 evidence fields (present on new writers; absent on old envelopes).
_COMMAND_EVIDENCE_OPTIONAL_SHAPE: Dict[str, tuple] = {
    "stdoutSha256": ("str",),
    "stderrSha256": ("str",),
    "stdoutTail": ("object", _COMMAND_TAIL_SHAPE),
    "stderrTail": ("object", _COMMAND_TAIL_SHAPE),
}
_COMMAND_EVIDENCE_KEYS = frozenset(_COMMAND_EVIDENCE_OPTIONAL_SHAPE.keys())

_VERIFIER_SHAPE: Dict[str, tuple] = {
    "identity": ("str",),
    "verdict": ("str",),
}

_ERROR_SHAPE: Dict[str, tuple] = {
    "class": ("enum", ERROR_CLASSES),
    "message": ("str",),
    "detail": ("object_or_null",),
}

_CLEANUP_SHAPE: Dict[str, tuple] = {
    "status": ("enum", CLEANUP_STATUSES),
    "detail": ("str_or_null",),
}

# Wave 1: optional citations for web-grounded runs.
_CITATION_SHAPE: Dict[str, tuple] = {"url": ("str",), "title": ("str",), "grounded": ("str",)}

# FIELD_SPECS: C4 schema. default_factory fills omissions; omit_when_absent skips
# the field entirely (verifier/error/citations when not applicable).

FIELD_SPECS: Dict[str, dict] = {
    "schemaVersion": {"type_spec": ("const_int", SCHEMA_VERSION), "default_factory": lambda: SCHEMA_VERSION},
    "runId": {"type_spec": ("str",), "default_factory": None},
    "mode": {"type_spec": ("enum", MODES), "default_factory": None},
    "status": {"type_spec": ("enum", STATUSES), "default_factory": None},
    "requestedModel": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "effectiveModel": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "repository": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "targetWorkspace": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "effectiveWorkingDirectory": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "baseRevision": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "worktreePath": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "worktreeBranch": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "sandbox": {
        "type_spec": ("object", _SANDBOX_SHAPE),
        "default_factory": lambda: {
            "requestedProfile": None,
            "reportedProfile": None,
            "enforced": None,
            "evidence": None,
        },
    },
    "policy": {
        "type_spec": ("object", _POLICY_SHAPE),
        "default_factory": lambda: {
            "tools": [],
            "permissionMode": None,
            "subagents": False,
            "webAccess": False,
            "memory": False,
        },
    },
    "instructions": {"type_spec": ("array_of_object", _INSTRUCTION_ITEM_SHAPE), "default_factory": lambda: []},
    "grok": {
        "type_spec": ("object", _GROK_SHAPE),
        "default_factory": lambda: {
            "sessionId": None,
            "requestId": None,
            "stopReason": None,
            "modelUsage": None,
        },
    },
    "usage": {"type_spec": ("object", _USAGE_SHAPE), "default_factory": lambda: {"turns": None, "raw": None}},
    "response": {"type_spec": ("object_or_str_or_null",), "default_factory": lambda: None},
    "changedFiles": {"type_spec": ("array_of_str",), "default_factory": lambda: []},
    "diffSummary": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "commands": {
        "type_spec": ("array_of_object", _COMMAND_ITEM_SHAPE, _COMMAND_EVIDENCE_OPTIONAL_SHAPE),
        "default_factory": lambda: [],
    },
    "verifier": {"type_spec": ("object", _VERIFIER_SHAPE), "default_factory": None, "omit_when_absent": True},
    "progressStreamPath": {"type_spec": ("str_or_null",), "default_factory": lambda: None},
    "warnings": {"type_spec": ("array_of_str",), "default_factory": lambda: []},
    "citations": {
        "type_spec": ("array_of_object", _CITATION_SHAPE),
        "default_factory": None,
        "omit_when_absent": True,
    },
    "error": {"type_spec": ("object", _ERROR_SHAPE), "default_factory": None, "omit_when_absent": True},
    "cleanup": {
        "type_spec": ("object", _CLEANUP_SHAPE),
        "default_factory": lambda: {"status": "not-applicable", "detail": None},
    },
    # Ephemeral durability signal for stdout-only failures (design §9.4).
    # When True, callers must not durable-persist this envelope. omit_when_absent
    # so normal durable envelopes stay free of the field.
    "doNotStore": {
        "type_spec": ("bool",),
        "default_factory": None,
        "omit_when_absent": True,
    },
}

# The four fields build_envelope binds directly from its named parameters;
# they are never legal as **fields keys (including under their own JSON
# name, e.g. "runId", which is not the same identifier as the "run_id"
# Python parameter and would otherwise slip through unnoticed).
_CORE_FIELD_NAMES = frozenset({"schemaVersion", "runId", "mode", "status"})
_ALLOWED_EXTRA_FIELD_NAMES = frozenset(FIELD_SPECS.keys()) - _CORE_FIELD_NAMES


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


def validate_envelope(candidate: dict) -> List[str]:
    """Structurally validate ``candidate`` against the C4 FIELD_SPECS table.

    Returns a list of human-readable violation strings; an empty list
    means the candidate is a valid C4 envelope. Never raises: this is a
    pure check function, used both internally by ``build_envelope`` and
    directly by callers that want to validate an envelope without
    constructing one (e.g. round-tripping a stored envelope.json, which is
    untrusted on-disk JSON and may not actually be a dict; the isinstance
    guard below fails that case closed with a violation instead of an
    AttributeError).
    """
    violations: List[str] = []

    if not isinstance(candidate, dict):
        return ["$: expected object, got {}".format(type(candidate).__name__)]

    for field_name, spec in FIELD_SPECS.items():
        omit_when_absent = spec.get("omit_when_absent", False)
        if field_name not in candidate:
            if not omit_when_absent:
                violations.append("{}: missing field".format(field_name))
            continue
        _validate_value(candidate[field_name], spec["type_spec"], field_name, violations)

    unknown_fields = set(candidate.keys()) - set(FIELD_SPECS.keys())
    for field_name in sorted(unknown_fields):
        violations.append("{}: unknown top-level field".format(field_name))

    return violations


def build_envelope(*, run_id: str, mode: str, status: str, **fields: object) -> dict:
    """Build and validate a complete C4 result envelope.

    ``run_id``, ``mode``, and ``status`` are required and fill the
    corresponding ``runId``/``mode``/``status`` JSON keys. Every other C4
    field may be supplied via ``**fields`` using its exact JSON key name
    (e.g. ``requestedModel=...``); any field not supplied is filled with
    its C4 default ("verifier" and "error" are the two exceptions: they
    are left out of the result entirely when not supplied, never set to
    null). Any key in ``**fields`` that is not a legal C4 field name
    (including a core field name smuggled in under its JSON key, or a
    field this function does not recognize at all) raises
    InvalidEnvelopeError before any other work happens. The fully-built
    candidate is then checked against FIELD_SPECS (raising
    InvalidEnvelopeError on any structural violation) and finally scanned
    for secret material (raising SecretMaterialError, a GrokWrapperError)
    before being returned.
    """
    disallowed_keys = set(fields.keys()) - _ALLOWED_EXTRA_FIELD_NAMES
    if disallowed_keys:
        _log_stderr(
            "build_envelope",
            "rejected unknown/disallowed envelope field(s): {}".format(sorted(disallowed_keys)),
        )
        raise InvalidEnvelopeError(
            "unknown or disallowed envelope field(s): {}".format(", ".join(sorted(disallowed_keys))),
            {"disallowedFields": sorted(disallowed_keys)},
        )

    candidate: Dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "mode": mode,
        "status": status,
    }

    for field_name, spec in FIELD_SPECS.items():
        if field_name in _CORE_FIELD_NAMES:
            continue
        if field_name in fields:
            candidate[field_name] = fields[field_name]
        elif spec.get("omit_when_absent", False):
            continue
        else:
            candidate[field_name] = spec["default_factory"]()

    violations = validate_envelope(candidate)
    if violations:
        _log_stderr("build_envelope", "candidate envelope failed C4 validation: {}".format(violations))
        raise InvalidEnvelopeError(
            "envelope failed C4 validation: {}".format("; ".join(violations)),
            {"violations": violations},
        )

    # D4(a): mask any EXACT occurrence of a credential value the wrapper injected
    # into this run's private Grok home (auth.json string leaves), regardless of
    # its shape, BEFORE the pattern scanner runs and before the envelope reaches
    # stdout/disk. This closes the case where Grok echoes its own injected auth
    # token in a form _SECRET_VALUE_PATTERNS cannot recognize. The injected
    # redaction runs IN ADDITION to assert_no_secret_material, never instead of
    # it: the pattern scanner below still runs as the fail-closed last line of
    # defense on the already injected-redacted candidate. The redactor is a no-op
    # when this run captured no injected credentials.
    redacted_candidate = injectedsecrets.redact_injected_secrets(candidate)

    assert_no_secret_material(redacted_candidate)

    return redacted_candidate


def failure_envelope(
    *,
    run_id: str,
    mode: str,
    error_class: str,
    message: str,
    detail: Optional[dict] = None,
    **fields: object,
) -> dict:
    """Build a status="failure" C4 envelope with its "error" field populated.

    ``error_class`` must be one of ``ERROR_CLASSES``; ``detail`` defaults
    to null (C4's ``error.detail`` is ``object|null``). Every other C4
    field can be supplied via ``**fields`` exactly as with
    ``build_envelope``; "status" and "error" are set directly by this
    function and must not also be passed via ``**fields``.
    """
    if error_class not in ERROR_CLASSES:
        # F1: error_class may carry dynamic, secret-shaped content (e.g. raw
        # stderr routed into this slot by a caller bug). Log and embed only a
        # value-free/redacted placeholder -- never the raw value -- since this
        # pre-check runs BEFORE assert_no_secret_material.
        safe_error_class = _safe_value_placeholder(error_class)
        _log_stderr("failure_envelope", "rejected unregistered error_class {}".format(safe_error_class))
        raise InvalidEnvelopeError(
            "error_class {} is not a registered C4 error class".format(safe_error_class),
            {"errorClass": safe_error_class, "allowedErrorClasses": list(ERROR_CLASSES)},
        )

    conflicting_fields = set(fields.keys()) & {"error", "status"}
    if conflicting_fields:
        raise InvalidEnvelopeError(
            "failure_envelope sets status and error directly; do not pass them via **fields",
            {"conflictingFields": sorted(conflicting_fields)},
        )

    # Grok dogfood-3 #5: error.message and error.detail carry dynamic text from
    # the raised GrokWrapperError (e.g. worktree._git puts raw git stderr into
    # detail). Redact BOTH through the shared value redactor BEFORE build_envelope
    # so any known secret shape is masked-and-reported rather than either riding
    # to stdout verbatim OR tripping the fail-closed scanner into dropping the
    # whole detail. The scanner still runs at the end of build_envelope as the
    # last-resort backstop for anything the pattern redactor does not recognize.
    safe_message = redact_secret_value_text(message)
    # redact_keys=True so a secret-shaped KEY in the caller's detail (round6
    # failure-detail-key-redaction: e.g. a benign "credentialDelegation" flag, or a
    # real opaque secret under "sessionToken") is renamed-and-suppressed here
    # rather than tripping build_envelope's fail-closed key scanner and DROPPING
    # the whole classified failure envelope. The scanner still runs afterwards.
    safe_detail = redact_secret_material(detail, redact_keys=True) if detail is not None else None
    error_field: Dict[str, object] = {"class": error_class, "message": safe_message, "detail": safe_detail}

    return build_envelope(run_id=run_id, mode=mode, status="failure", error=error_field, **fields)


def exit_code_for(envelope: dict) -> int:
    """Return 0 for a non-failure envelope, else 1.

    ``success`` and ``running`` (status-mode: target still in progress) both
    exit 0 so a successful poll of a live run is not treated as a command error.
    """
    return 0 if envelope.get("status") in ("success", "running") else 1


def emit_envelope(envelope: dict, envelope_path: Optional[pathlib.Path]) -> None:
    """Print exactly one line-terminated JSON document for ``envelope`` to stdout.

    When ``envelope_path`` is not None, a stored copy is written first at
    that path with mode 0600 (pre-run failures pass ``envelope_path=None``
    and store no copy, per C8). Ephemeral ``doNotStore`` is kept on stdout but
    stripped from any stored copy so durable files never carry the signal.
    This function contains the single stdout write in the entire groklib package
    (Global Constraints stdout discipline: wrapper subcommands write exactly one
    JSON result envelope to stdout and nothing else).
    """
    serialized = json.dumps(envelope, sort_keys=True)

    if envelope_path is not None:
        # Never durable-store an ephemeral durability marker.
        to_store = {k: v for k, v in envelope.items() if k != "doNotStore"}
        store_serialized = json.dumps(to_store, sort_keys=True)
        try:
            file_descriptor = os.open(
                str(envelope_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _STORED_ENVELOPE_FILE_MODE
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(store_serialized + "\n")
            # P4: route the owner-only tightening through platformsupport so it
            # is consistent (POSIX chmod / Windows ACL) rather than a raw chmod.
            platformsupport.restrict_file_permissions(envelope_path)
        except OSError as exc:
            _log_stderr("emit_envelope", "failed writing stored envelope copy {}: {}".format(envelope_path, exc))
            raise

    print(serialized)
