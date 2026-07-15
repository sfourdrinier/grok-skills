# wrapper/scripts/groklib/modes/status.py
#
# `status --run-id` mode: a strictly READ-ONLY inspector that loads a stored
# run's run.json, its stored C4 envelope, and its C3 progress stream, and
# returns a fresh envelope whose `response` embeds
# {storedEnvelope, events, eventWarnings}. It writes NOTHING to the target run
# directory. An unknown or malformed run id is `invalid-target`; a stored
# envelope that is unreadable, not JSON, or not a valid C4 document is
# `output-malformed` (the malformed document is never re-emitted verbatim).

import argparse
import json
import pathlib
from typing import List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import envelope as envelope_mod
from groklib.progress import read_events


def _log(function: str, message: str) -> None:
    log_stderr("modes.status", function, message)


def _load_stored_envelope(
    run_id: str, stored_path: pathlib.Path, warnings: List[str]
) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (stored_envelope, failure_envelope).

    Exactly one is non-None: a readable, valid C4 stored envelope (or None with
    a warning when the file is simply absent), or a classified
    `output-malformed` failure envelope when the file is unreadable, not JSON,
    or fails C4 validation (obligation b: never re-emit a malformed doc).
    """
    if not stored_path.exists():
        warnings.append("stored envelope not found for run {}".format(run_id))
        return None, None

    try:
        raw = stored_path.read_text(encoding="utf-8")
        stored = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        _log("_load_stored_envelope", "stored envelope unreadable/not JSON for {}: {}".format(run_id, exc))
        return None, envelope_mod.failure_envelope(
            run_id=run_id,
            mode="status",
            error_class="output-malformed",
            message="stored envelope for run {} is unreadable or not valid JSON".format(run_id),
            detail={"runId": run_id},
            progressStreamPath=None,
        )

    violations = envelope_mod.validate_envelope(stored)
    if violations:
        _log("_load_stored_envelope", "stored envelope for {} failed C4 validation".format(run_id))
        return None, envelope_mod.failure_envelope(
            run_id=run_id,
            mode="status",
            error_class="output-malformed",
            message="stored envelope for run {} failed C4 validation".format(run_id),
            detail={"violations": violations},
            progressStreamPath=None,
        )

    return stored, None


def _stream_redact_event_text(events: List[dict]) -> List[dict]:
    """Redact each event's ``data.text`` as one continuous stream (cross-event secret split).

    The streaming coalescer writes Grok's raw tokens into event ``data.text``
    chunks; a secret can be split across two consecutive chunks. Collect those
    text values in event order, redact them as a single joined stream via
    ``envelope.redact_secret_text_stream`` (so a boundary-spanning secret is
    masked in both halves), and return NEW event dicts with the redacted text.
    Events without a string ``data.text`` are passed through unchanged.
    """
    texts: List[str] = [
        event["data"]["text"]
        for event in events
        if isinstance(event.get("data"), dict) and isinstance(event["data"].get("text"), str)
    ]
    if not texts:
        return events
    redacted_iter = iter(envelope_mod.redact_secret_text_stream(texts))
    rebuilt: List[dict] = []
    for event in events:
        data = event.get("data")
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            new_data = dict(data)
            new_data["text"] = next(redacted_iter)
            new_event = dict(event)
            new_event["data"] = new_data
            rebuilt.append(new_event)
        else:
            rebuilt.append(event)
    return rebuilt


def run(args: argparse.Namespace) -> dict:
    """Load and report the stored run identified by ``args.run_id`` (read-only)."""
    run_id = args.run_id
    try:
        runstate.load_run_record(run_id)
    except GrokWrapperError as exc:
        _log("run", "cannot load run record for {!r}: {} ({})".format(run_id, exc.error_class, exc))
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="status",
            error_class=exc.error_class,
            message=str(exc),
            detail=exc.detail or None,
            progressStreamPath=None,
        )

    run_dir = runstate.state_root() / "runs" / run_id
    # PR968 codex status-ownership: a valid run.json is NOT proof the run dir is a
    # genuine wrapper-owned target -- cleanup verifies the owner.json marker (and that
    # it names the requested run id) before acting, and status must too. Without it a
    # corrupt/strict-shaped dir with a valid run.json but a missing/mismatched owner
    # marker would return a SUCCESS envelope the companion then renders. Fail closed as
    # invalid-target (an unknown/malformed target, same class the unknown-run path uses)
    # so the companion renders nothing.
    try:
        owner_run_id = runstate.verify_owner_marker(run_dir / "owner.json")
    except GrokWrapperError as exc:
        _log("run", "run dir ownership marker invalid for {!r}: {} ({})".format(run_id, exc.error_class, exc))
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="status",
            error_class="invalid-target",
            message=str(exc),
            detail=exc.detail or None,
            progressStreamPath=None,
        )
    if owner_run_id != run_id:
        _log("run", "run dir marker run id {!r} does not match requested {!r}".format(owner_run_id, run_id))
        return envelope_mod.failure_envelope(
            run_id=str(run_id),
            mode="status",
            error_class="invalid-target",
            message="run dir ownership marker run id does not match the requested run id",
            detail={"markerRunId": owner_run_id, "requestedRunId": run_id},
            progressStreamPath=None,
        )

    stored_path = run_dir / "envelope.json"
    progress_path = run_dir / "progress.jsonl"

    warnings: List[str] = []
    stored, failure = _load_stored_envelope(run_id, stored_path, warnings)
    if failure is not None:
        return failure

    events, event_warnings = read_events(progress_path)
    # Grok dogfood-3 #6: the streaming coalescer batches raw thought/text tokens
    # into ~480-char event `data.text` chunks, so a credential can be SPLIT across
    # two consecutive events -- no single event matches a secret pattern, but the
    # concatenation is the secret. Per-event redaction alone would miss it. Redact
    # the event `data.text` values as ONE continuous stream FIRST (so a boundary-
    # spanning secret is caught), then run the per-leaf redactor below over the
    # whole response for every other embedded string.
    safe_events = _stream_redact_event_text(events)
    response = {"storedEnvelope": stored, "events": safe_events, "eventWarnings": event_warnings}
    # D-STREAM regression fix (F-STATUS-SECRET): progress.jsonl now carries Grok's
    # raw thought/text tokens, so an embedded event whose text mentions a
    # "bearer "/JWT/sk- shape would otherwise trip build_envelope's
    # assert_no_secret_material and PERMANENTLY fail readback of that run. Redact
    # the secret-shaped substrings in the EMBEDDED (stdout) copy only, using the
    # same patterns the scanner enforces (envelope.redact_secret_material). The
    # on-disk progress.jsonl stays raw inside the private 0700 run dir; this
    # inspector still writes nothing to the run directory.
    safe_response = envelope_mod.redact_secret_material(response)
    return envelope_mod.build_envelope(
        run_id=run_id,
        mode="status",
        status="success",
        progressStreamPath=str(progress_path),
        warnings=warnings,
        response=safe_response,
    )
