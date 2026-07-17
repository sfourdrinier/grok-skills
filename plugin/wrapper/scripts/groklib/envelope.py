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

# Redaction primitives live in groklib.redaction (900-line cap). Re-exported
# so every existing "from groklib.envelope import redact_*" site keeps working.
from groklib.redaction import (  # noqa: F401
    SecretMaterialError,
    assert_no_secret_material,
    redact_secret_material,
    redact_secret_text_stream,
    redact_secret_value_text,
)

# Private names some tests / local helpers may still reference via envelope.
from groklib.redaction import (  # noqa: F401
    _SECRET_VALUE_PATTERNS,
    _is_secret_shaped_key,
)

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
    # 2.0.0: TAP "not ok" lines from the FULL stdout of a FAILED command,
    # captured before tail truncation so validation failures name their tests.
    "failedTests": ("array_of_str",),
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
