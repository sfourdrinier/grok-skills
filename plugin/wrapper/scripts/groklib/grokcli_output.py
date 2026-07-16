# wrapper/scripts/groklib/grokcli_output.py
#
# Pure parsing, extraction, and validation of Grok CLI output. This module
# owns everything grokcli.py needs to turn raw child-process bytes into
# classified facts, with zero subprocess or filesystem concerns of its own,
# so the process-orchestration logic in grokcli.py stays small and this
# output surface can be unit-tested directly against the Task 0 fixture
# shapes (real-output-shape.json, inspect-shape.json, the `grok models`
# transcript in probe-report.md).
#
# Every failure fails closed with the exact C4 error class:
#   - unparseable / non-object JSON  -> output-malformed
#   - structured output that a schema requires but is missing/wrong -> schema-mismatch
#   - `grok models` reporting no login line -> auth-missing
# No file contents are logged; only structural facts (JSON error text, JSON
# pointers, type names) appear in diagnostics.

import json
import re
from typing import Dict, List, Optional, TypedDict

from groklib import GrokWrapperError, log_stderr

# The stop-reason tokens (normalized: lowercased, non-alphanumerics stripped)
# that classify a run as cancelled or turn-exhausted. Task 0 (probe-report.md
# Step 3/4) observed EndTurn and Cancelled directly; the max-turn stop reason
# string was not captured live (only EndTurn/Cancelled appeared), so the
# turn-exhaustion matcher is deliberately robust: a token-set match plus a
# "maxturn" substring match plus a num_turns-at-budget fallback. Task 13's
# live revalidation suite can pin the exact real token if it ever differs.
_CANCELLED_STOP_TOKENS = frozenset({"cancelled", "canceled", "aborted", "userabort", "interrupted"})
_TURN_EXHAUSTION_STOP_TOKENS = frozenset(
    {"maxturns", "maxturnsreached", "maxturnsexceeded", "turnlimit", "maxturnlimit", "maxstepsreached"}
)
_CLEAN_TERMINAL_STOP_TOKENS = frozenset({"endturn"})
# Substrings that mark a stop reason as an ERROR/refusal terminal rather than a
# turn-budget terminal. The num_turns-at-budget FALLBACK below must NOT reclassify
# such an error stop as turn-exhaustion just because it happened to land at the
# turn cap (Grok dogfood #13): an explicit error/refusal stop stays a cli-failure.
_ERROR_STOP_SUBSTRINGS = ("error", "fail", "refus", "denied", "invalid")

# The JSON-Schema subset the structured-output walker understands (D-WEB /
# Task 7 Step 4). Anything outside this set the walker cannot prove and
# therefore rejects as schema-mismatch.
_SCALAR_TYPES = ("string", "number", "integer", "boolean")

# The schema KEYWORDS the walker actually enforces. PR968 codex #6: any keyword
# outside this set that CONSTRAINS validation (additionalProperties, minProperties,
# patternProperties, minLength, pattern, oneOf, ...) must fail closed rather than be
# silently ignored -- otherwise an operator --schema relying on it would accept output
# the schema forbids.
_ENFORCED_SCHEMA_KEYWORDS = frozenset({"type", "enum", "required", "properties", "items"})
# Pure annotation/metadata keywords that never constrain instance validation, so
# ignoring them can never let a forbidden value pass; they are allowed through.
_INERT_SCHEMA_KEYWORDS = frozenset(
    {"$schema", "$id", "$comment", "title", "description", "default", "examples", "readOnly", "writeOnly", "deprecated"}
)

# `grok models` login marker (probe-report.md Step 2: "You are logged in
# with grok.com."). Matched case-insensitively at line start.
_LOGGED_IN_PREFIX = "you are logged in"


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "grokcli_output" component."""
    log_stderr("grokcli_output", function, message)


def parse_grok_json(stdout: str) -> Dict[str, object]:
    """Parse Grok's stdout as a single JSON object, failing closed as output-malformed.

    An empty/whitespace-only stdout, non-JSON text, or a JSON value that is
    not an object all raise ``GrokWrapperError("output-malformed")``: a JSON
    document is expected, and anything else is unparseable output for the
    purposes of the C4 classification.
    """
    stripped = stdout.strip()
    if not stripped:
        raise GrokWrapperError(
            "output-malformed",
            "grok produced no JSON document to parse",
            {"reason": "empty-output"},
        )
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        _log("parse_grok_json", "grok stdout was not valid JSON: {}".format(exc))
        raise GrokWrapperError(
            "output-malformed",
            "grok stdout was not valid JSON: {}".format(exc),
            {"jsonError": str(exc)},
        )
    if not isinstance(parsed, dict):
        _log("parse_grok_json", "grok stdout JSON was a {} not an object".format(type(parsed).__name__))
        raise GrokWrapperError(
            "output-malformed",
            "grok stdout JSON was a {} not an object".format(type(parsed).__name__),
            {"jsonType": type(parsed).__name__},
        )
    return parsed


def _normalize_stop_reason(value: Optional[object]) -> str:
    """Lowercase ``value`` and strip every non-alphanumeric character; non-strings become ""."""
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.lower() if character.isalnum())


def is_cancelled(stop_reason: Optional[object]) -> bool:
    """True when ``stop_reason`` normalizes to a cancellation token (Task 0: "Cancelled")."""
    return _normalize_stop_reason(stop_reason) in _CANCELLED_STOP_TOKENS


def is_turn_exhaustion(
    stop_reason: Optional[object], num_turns: Optional[object], max_turns: Optional[int]
) -> bool:
    """True when the run hit an operator-set turn budget.

    Turn-exhaustion only applies when the operator set ``max_turns`` (default:
    unlimited / flag omitted). Without that budget, this never returns True -
    even if Grok emits a MaxTurns-shaped stop token (platform/internal limit is
    not reclassified as operator turn-exhaustion).

    Primary signal (with budget set): stop reason normalizes to a known max-turn
    token or contains "maxturn".

    Grok CLI often reports the turn cap as stopReason ``Cancelled`` (not a
    distinct max-turn token). When the operator set ``max_turns`` and
    ``num_turns >= max_turns``, treat Cancelled (and other non-clean, non-error
    stops at the budget) as turn-exhaustion - not a plain user abort.

    Cancelled with *missing* ``num_turns`` is never treated as budget: that
    mislabels real mid-run cancels when the operator merely set ``--max-turns``.
    Require turn-count evidence.

    An explicit error/refusal stop that coincides with the turn cap is NOT
    reclassified (Grok dogfood #13).
    """
    if not isinstance(max_turns, int) or isinstance(max_turns, bool):
        return False
    normalized = _normalize_stop_reason(stop_reason)
    if normalized in _TURN_EXHAUSTION_STOP_TOKENS or "maxturn" in normalized:
        return True
    if any(error_token in normalized for error_token in _ERROR_STOP_SUBSTRINGS):
        return False
    if normalized in _CLEAN_TERMINAL_STOP_TOKENS:
        return False
    turns_ok = isinstance(num_turns, int) and not isinstance(num_turns, bool)
    if turns_ok and num_turns >= max_turns:
        # Includes Cancelled at the budget (Grok's observed turn-cap stop token).
        return True
    return False


def _finding_has_content(item: object) -> bool:
    """True when a findings[] entry has non-placeholder textual content."""
    if not isinstance(item, dict):
        return bool(item)
    for key in ("title", "message", "detail", "summary", "description", "body", "file", "path"):
        val = item.get(key)
        if isinstance(val, str) and val.strip() and val.strip().lower() != "placeholder":
            return True
    # severity alone is not content; any other non-empty string field counts
    for key, val in item.items():
        if key in ("severity", "level", "id"):
            continue
        if isinstance(val, str) and val.strip() and val.strip().lower() != "placeholder":
            return True
    return False


def has_usable_model_output(fields: "dict") -> bool:
    """True when final text and/or structuredOutput is worth keeping on an incomplete stop.

    Empty shells (blank text, empty dict/list, findings:[] / findings:null only)
    are not usable: salvaging those would green-light an empty incomplete review.
    """
    text = fields.get("final_text") if isinstance(fields, dict) else None
    if isinstance(text, str) and text.strip():
        return True
    structured = fields.get("structured") if isinstance(fields, dict) else None
    if structured is None:
        return False
    if isinstance(structured, str) and structured.strip():
        return True
    if isinstance(structured, list):
        return len(structured) > 0
    if isinstance(structured, dict):
        if not structured:
            return False
        if "findings" in structured:
            findings = structured.get("findings")
            # findings key present: only a non-empty list of contentful items counts.
            # null / object / string / empty list are empty shells (not usable alone).
            if not isinstance(findings, list) or not findings:
                return False
            return any(_finding_has_content(item) for item in findings)
        # No findings key: non-empty dict with other payload (e.g. reason answer).
        return True
    return False


def _effective_model_from_usage(model_usage: Optional[object]) -> Optional[str]:
    """Return the effective model id keyed by ``modelUsage`` (Task 0: grok-4.5 or grok-4.5-build)."""
    if isinstance(model_usage, dict) and model_usage:
        for key in model_usage:
            if isinstance(key, str):
                return key
    return None


class ResultFields(TypedDict):
    """The typed shape of extract_result_fields' output (T8).

    A TypedDict so a key rename becomes a static type error at the consumer
    (grokcli.execute) rather than a runtime KeyError.
    """

    stop_reason: Optional[str]
    session_id: Optional[str]
    request_id: Optional[str]
    model_usage: Optional[Dict[str, object]]
    effective_model: Optional[str]
    final_text: Optional[str]
    structured: Optional[object]
    num_turns: Optional[int]


def extract_result_fields(parsed: Dict[str, object]) -> ResultFields:
    """Pull the C4-relevant fields out of a parsed Grok JSON document (Task 0 shape mapping).

    Field mapping (probe-report.md Step 3): grok.stopReason <- stopReason,
    grok.sessionId <- sessionId, grok.requestId <- requestId, grok.modelUsage
    <- modelUsage, usage.turns <- num_turns, response/final text <- text,
    structured <- structuredOutput. Type-mismatched values are surfaced as
    None rather than trusted blindly.

    PR968 codex #5: ``structuredOutput`` is preserved with its ORIGINAL JSON
    type (object, array, or scalar), not coerced to None when it is not a dict.
    ``validate_structured_output`` accepts array and scalar roots, so discarding
    a non-object here would misreport a valid array-root result as
    structured-output-missing. Only an ABSENT key or a JSON ``null`` maps to
    None (both mean "no structured output" for the schema-run missing check).
    """
    stop_reason = parsed.get("stopReason")
    session_id = parsed.get("sessionId")
    request_id = parsed.get("requestId")
    model_usage = parsed.get("modelUsage")
    final_text = parsed.get("text")
    structured = parsed.get("structuredOutput")
    num_turns = parsed.get("num_turns")
    if num_turns is None:
        num_turns = parsed.get("numTurns")
    if num_turns is None and isinstance(parsed.get("usage"), dict):
        usage_obj = parsed["usage"]
        num_turns = usage_obj.get("turns") or usage_obj.get("num_turns") or usage_obj.get("numTurns")

    turns_value = None
    if isinstance(num_turns, int) and not isinstance(num_turns, bool):
        turns_value = num_turns
    elif isinstance(num_turns, float) and num_turns == int(num_turns):
        turns_value = int(num_turns)

    return {
        "stop_reason": stop_reason if isinstance(stop_reason, str) else None,
        "session_id": session_id if isinstance(session_id, str) else None,
        "request_id": request_id if isinstance(request_id, str) else None,
        "model_usage": model_usage if isinstance(model_usage, dict) else None,
        "effective_model": _effective_model_from_usage(model_usage),
        "final_text": final_text if isinstance(final_text, str) else None,
        "structured": structured,
        "num_turns": turns_value,
    }


def _pointer_token(key: str) -> str:
    """Escape a key into an RFC 6901 JSON-pointer reference token (~ -> ~0, / -> ~1)."""
    return key.replace("~", "~0").replace("/", "~1")


def _child_pointer(pointer: str, key: str) -> str:
    """Append one escaped ``key`` to a JSON pointer."""
    return "{}/{}".format(pointer, _pointer_token(key))


def _raise_schema_mismatch(pointer: str, message: str, detail_extra: Dict[str, object]) -> None:
    """Raise schema-mismatch with the failing JSON pointer (root pointer rendered as "/")."""
    detail: Dict[str, object] = {"pointer": pointer if pointer else "/"}
    detail.update(detail_extra)
    _log("validate_structured_output", "{} at pointer {}".format(message, detail["pointer"]))
    raise GrokWrapperError("schema-mismatch", message, detail)


def _is_number(instance: object) -> bool:
    """True for a JSON number (int or float) but not bool (which is an int subclass in Python)."""
    return isinstance(instance, (int, float)) and not isinstance(instance, bool)


def _is_integer(instance: object) -> bool:
    """True for a JSON integer but not bool."""
    return isinstance(instance, int) and not isinstance(instance, bool)


def _validate_scalar(schema_type: str, instance: object, pointer: str) -> None:
    """Validate a scalar ``instance`` against a recognized scalar ``schema_type``."""
    if schema_type == "string" and not isinstance(instance, str):
        _raise_schema_mismatch(pointer, "expected string", {"expectedType": "string"})
    if schema_type == "number" and not _is_number(instance):
        _raise_schema_mismatch(pointer, "expected number", {"expectedType": "number"})
    if schema_type == "integer" and not _is_integer(instance):
        _raise_schema_mismatch(pointer, "expected integer", {"expectedType": "integer"})
    if schema_type == "boolean" and not isinstance(instance, bool):
        _raise_schema_mismatch(pointer, "expected boolean", {"expectedType": "boolean"})


def _enum_allows(allowed: List[object], instance: object) -> bool:
    """True when ``instance`` equals one ``allowed`` value with a matching JSON type.

    Uses type-aware equality so a JSON boolean is never accepted for an integer
    enum (Python's ``True == 1`` would otherwise let ``1`` satisfy ``enum: [true]``
    and vice versa); int/float are treated as one numeric kind since JSON does not
    distinguish them.
    """
    for candidate in allowed:
        candidate_is_bool = isinstance(candidate, bool)
        instance_is_bool = isinstance(instance, bool)
        if candidate_is_bool != instance_is_bool:
            continue
        candidate_is_number = _is_number(candidate)
        instance_is_number = _is_number(instance)
        if candidate_is_number and instance_is_number:
            if candidate == instance:
                return True
            continue
        if type(candidate) is type(instance) and candidate == instance:
            return True
    return False


def validate_structured_output(instance: object, schema: object, pointer: str = "") -> None:
    """Recursively validate ``instance`` against the JSON-Schema subset ``schema``.

    Supported keywords: ``type`` (object, array, string, number, integer,
    boolean), ``enum`` (any node), ``required`` (object), ``properties``
    (object), ``items`` (array). Anything the walker cannot prove - a
    non-object schema node, a missing/unsupported ``type``, a value not in a
    declared ``enum``, a missing required property, or a value whose runtime
    type does not match - raises ``GrokWrapperError("schema-mismatch")``
    carrying the failing JSON pointer in ``detail["pointer"]``. Fail closed: an
    unrecognized ``type`` is a mismatch, never a silent pass. A structurally
    invalid object schema node - ``required`` present but not a list,
    ``properties`` present but not an object, or ``enum`` present but not a
    list - is likewise a fail-loud ``schema-mismatch`` at that node's pointer,
    never a silent under-validation of a malformed operator ``--schema``.

    PR968 codex #6: any keyword the walker does NOT enforce and that is not a pure
    annotation keyword (e.g. ``additionalProperties``, ``minProperties``,
    ``patternProperties``, ``minLength``, ``pattern``, ``oneOf``) is a fail-closed
    ``schema-mismatch`` at that node's pointer. Silently ignoring a constraint
    keyword would let output the operator's ``--schema`` forbids pass validation.
    """
    if not isinstance(schema, dict):
        _raise_schema_mismatch(pointer, "schema node is not an object", {"schemaType": type(schema).__name__})

    unsupported = sorted(
        key for key in schema if key not in _ENFORCED_SCHEMA_KEYWORDS and key not in _INERT_SCHEMA_KEYWORDS
    )
    if unsupported:
        _raise_schema_mismatch(
            pointer,
            "schema node uses unsupported keyword(s): {}".format(unsupported),
            {"reason": "unsupported-schema-keyword", "unsupportedKeywords": unsupported},
        )

    schema_type = schema.get("type")

    if "enum" in schema:
        allowed = schema.get("enum")
        if not isinstance(allowed, list):
            _raise_schema_mismatch(
                pointer,
                "schema node 'enum' must be a list",
                {"reason": "schema-enum-not-a-list", "enumType": type(allowed).__name__},
            )
        if not _enum_allows(allowed, instance):
            _raise_schema_mismatch(
                pointer,
                "value is not permitted by the schema enum",
                {"reason": "enum-mismatch"},
            )
        if schema_type is None:
            # An enum-only node (no ``type``) is fully validated by the membership
            # check above; do not fall through to the missing-type mismatch below.
            return

    if schema_type == "object":
        if not isinstance(instance, dict):
            _raise_schema_mismatch(pointer, "expected object", {"expectedType": "object"})
        pointer_display = pointer if pointer else "/"

        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if not isinstance(key, str):
                    # A malformed "required" ENTRY (operator --schema supplied a
                    # non-string list item) must fail loudly. Silently skipping
                    # it would validate the instance against a required-key list
                    # the operator did not actually author -- the same fail-closed
                    # treatment the non-list "required" node below already gets.
                    _raise_schema_mismatch(
                        pointer,
                        "schema node {}: 'required' entries must be property-name strings".format(
                            pointer_display
                        ),
                        {"reason": "schema-required-entry-not-a-string", "entryType": type(key).__name__},
                    )
                if key not in instance:
                    _raise_schema_mismatch(
                        _child_pointer(pointer, key),
                        "required property missing",
                        {"reason": "required-property-missing"},
                    )
        elif "required" in schema:
            # A structurally invalid schema node (operator-supplied --schema
            # with a malformed "required") must fail loudly rather than
            # silently skip the required-property check.
            _raise_schema_mismatch(
                pointer,
                "schema node {}: 'required' must be a list".format(pointer_display),
                {"reason": "schema-required-not-a-list", "requiredType": type(required).__name__},
            )

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if isinstance(key, str) and isinstance(instance, dict) and key in instance:
                    validate_structured_output(instance[key], subschema, _child_pointer(pointer, key))
        elif "properties" in schema:
            # Same fail-loud treatment for a malformed "properties" node.
            _raise_schema_mismatch(
                pointer,
                "schema node {}: 'properties' must be an object".format(pointer_display),
                {"reason": "schema-properties-not-an-object", "propertiesType": type(properties).__name__},
            )
        return

    if schema_type == "array":
        if not isinstance(instance, list):
            _raise_schema_mismatch(pointer, "expected array", {"expectedType": "array"})
        items = schema.get("items")
        if isinstance(items, dict) and isinstance(instance, list):
            for index, element in enumerate(instance):
                validate_structured_output(element, items, _child_pointer(pointer, str(index)))
        elif "items" in schema:
            # A tuple-form items ([...]) or items:false/true is an item constraint
            # this validator does not implement. Fail closed rather than silently
            # accept every array element, consistent with the unsupported-keyword
            # handling above -- an unimplemented constraint must never validate.
            _raise_schema_mismatch(
                pointer,
                "schema node 'items' must be an object schema",
                {"reason": "schema-items-not-an-object", "itemsType": type(items).__name__},
            )
        return

    if schema_type in _SCALAR_TYPES:
        _validate_scalar(schema_type, instance, pointer)
        return

    _raise_schema_mismatch(
        pointer,
        "unsupported or missing schema type",
        {"schemaType": schema_type},
    )


def parse_models_output(stdout: str) -> Dict[str, object]:
    """Parse `grok models` text into {loggedIn, defaultModel, models} (probe-report.md Step 2).

    Login is keyed off the "You are logged in..." line; its absence is
    ``auth-missing``. A logged-in transcript that lacks a parseable default
    model or model list is ``output-malformed``. Model ids are read from the
    bulleted "Available models:" section (lines starting with ``*`` or ``-``).
    """
    lines = stdout.splitlines()
    logged_in = any(line.strip().lower().startswith(_LOGGED_IN_PREFIX) for line in lines)
    if not logged_in:
        _log("parse_models_output", "grok models did not report a logged-in session")
        raise GrokWrapperError(
            "auth-missing",
            "grok models reports the private home is not logged in",
            {"reason": "not-logged-in"},
        )

    default_model: Optional[str] = None
    for line in lines:
        match = re.match(r"\s*default model:\s*(\S+)", line, re.IGNORECASE)
        if match:
            default_model = match.group(1)
            break

    models: List[str] = []
    for line in lines:
        match = re.match(r"\s*[\*\-]\s+(\S+)", line)
        if match:
            models.append(match.group(1))

    if default_model is None or not models:
        _log("parse_models_output", "grok models output missing default model or model list")
        raise GrokWrapperError(
            "output-malformed",
            "grok models output did not carry a parseable default model and model list",
            {"reason": "unparseable-models-output"},
        )

    return {"loggedIn": True, "defaultModel": default_model, "models": models}
