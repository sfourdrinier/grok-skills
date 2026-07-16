# wrapper/scripts/groklib/modes/status.py
#
# `status --run-id` mode: a strictly READ-ONLY inspector that loads a stored
# run's run.json, its stored C4 envelope (if finished), and its C3 progress
# stream, and returns a fresh envelope whose `response` embeds
# {storedEnvelope, events, eventWarnings, target}. It writes NOTHING to the
# target run directory.
#
# Outcome of the status *query* (top-level envelope status projection, design §6):
#   - running (exit 0): target lifecycle created|running|finalizing
#   - success (exit 0): target lifecycle completed
#   - failure (exit 1): failed|canceled|derived interrupted, or load/own/malformed
# Target-failure projection has response.target (no error field). Command failure
# uses failure_envelope with error.class. Status never writes the run directory.

import argparse
import datetime
import json
import pathlib
from typing import List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import envelope as envelope_mod
from groklib.progress import read_events


def _log(function: str, message: str) -> None:
    log_stderr("modes.status", function, message)


def _load_stored_envelope(
    run_id: str, stored_path: pathlib.Path
) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (stored_envelope, failure_envelope).

    - File absent → (None, None)  # caller decides running vs incomplete
    - Unreadable / not JSON / invalid C4 → (None, failure_envelope)
    - Valid C4 → (stored, None)
    """
    if not stored_path.exists():
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

    # Bind stored envelope to the requested run (never project a foreign runId).
    if stored.get("runId") != run_id:
        _log(
            "_load_stored_envelope",
            "stored envelope runId {!r} does not match requested {!r}".format(
                stored.get("runId"), run_id
            ),
        )
        return None, envelope_mod.failure_envelope(
            run_id=run_id,
            mode="status",
            error_class="output-malformed",
            message="stored envelope for run {} belongs to a different runId".format(run_id),
            detail={"requestedRunId": run_id, "storedRunId": stored.get("runId")},
            progressStreamPath=None,
        )

    return stored, None


def _stream_redact_event_text(events: List[dict]) -> List[dict]:
    """Redact each event's ``data.text`` as one continuous stream (cross-event secret split)."""
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


def _parse_utc(ts: Optional[str]) -> Optional[datetime.datetime]:
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_summary(event: dict) -> dict:
    """Compact summary of one progress event for operators (no full thought dumps)."""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    summary = {
        "seq": event.get("seq"),
        "phase": event.get("phase"),
        "level": event.get("level"),
        "message": event.get("message"),
        "ts": event.get("ts"),
    }
    if isinstance(data.get("event"), str):
        summary["event"] = data.get("event")
    if isinstance(data.get("chars"), int):
        summary["chars"] = data.get("chars")
    # Short preview of stream text (already redacted when called after stream redact)
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        preview = text.strip().replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:157] + "..."
        summary["textPreview"] = preview
    return summary


def _build_target_info(
    *,
    record: dict,
    run_dir: pathlib.Path,
    events: List[dict],
    process_liveness: str,
    has_stored_envelope: bool,
    effective_life: str,
    lifecycle_source: str,
) -> dict:
    created = record.get("createdAtUtc")
    created_dt = _parse_utc(created if isinstance(created, str) else None)
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed_seconds = None
    elapsed_ms = 0
    if created_dt is not None:
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=datetime.timezone.utc)
        delta = now - created_dt
        # Wall-clock for status display (design §8); clamp negative to 0
        elapsed_seconds = max(0, int(delta.total_seconds()))
        elapsed_ms = max(0, int(delta.total_seconds() * 1000))

    last_event = _event_summary(events[-1]) if events else None
    last_progress_at = None
    if last_event is not None and events:
        # lastEvent may carry process-local elapsedMs from the progress writer
        last_em = events[-1].get("elapsedMs")
        if isinstance(last_em, int) and last_em >= 0:
            last_event["elapsedMs"] = last_em
        ts = events[-1].get("ts")
        if isinstance(ts, str):
            last_progress_at = ts
    recent = [_event_summary(ev) for ev in events[-8:]] if events else []

    return {
        "mode": record.get("mode"),
        "lifecycle": effective_life,
        "lifecycleSource": lifecycle_source,
        "recordStatus": record.get("status"),
        "process": process_liveness,  # alive | dead | unknown
        "hasStoredEnvelope": has_stored_envelope,
        "resultAvailable": has_stored_envelope,
        "createdAtUtc": created,
        "elapsedSeconds": elapsed_seconds,
        "elapsedMs": elapsed_ms,
        "lastProgressAt": last_progress_at,
        "requestedModel": record.get("requestedModel"),
        "repository": record.get("repository"),
        "eventCount": len(events),
        "lastEvent": last_event,
        "recentEvents": recent,
        "runDir": str(run_dir),
    }


def run(args: argparse.Namespace) -> dict:
    """Load and report the stored run identified by ``args.run_id`` (read-only)."""
    run_id = args.run_id
    try:
        record = runstate.load_run_record(run_id)
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
    stored, failure = _load_stored_envelope(run_id, stored_path)
    if failure is not None:
        return failure

    events, event_warnings = read_events(progress_path)
    safe_events = _stream_redact_event_text(events)
    process_liveness = runstate._home_owner_liveness(run_dir)
    # Parent owner.pid can die while the finalize worker still lives and may
    # still persist the real terminal envelope. Treat that as process-alive so
    # finalizing runs project as in-progress, not derived interrupted.
    try:
        from groklib.modes.finalize_worker import finalize_worker_blocks_durable_write

        # alive or unknown: project as still in progress (never derived interrupted).
        if finalize_worker_blocks_durable_write(run_dir):
            process_liveness = "alive"
    except Exception as liveness_exc:
        _log(
            "run",
            "finalize worker liveness check failed (treating as alive): {}".format(liveness_exc),
        )
        process_liveness = "alive"
    record_status = record.get("status") if isinstance(record.get("status"), str) else None
    envelope_status = stored.get("status") if isinstance(stored, dict) else None
    envelope_error_class = None
    if isinstance(stored, dict) and isinstance(stored.get("error"), dict):
        ec = stored["error"].get("class")
        if isinstance(ec, str):
            envelope_error_class = ec
    effective_life, lifecycle_source = runstate.effective_lifecycle(
        record,
        has_valid_envelope=stored is not None,
        envelope_status=envelope_status if isinstance(envelope_status, str) else None,
        process_liveness=process_liveness,
        envelope_error_class=envelope_error_class,
    )

    target = _build_target_info(
        record=record,
        run_dir=run_dir,
        events=safe_events,
        process_liveness=process_liveness,
        has_stored_envelope=stored is not None,
        effective_life=effective_life,
        lifecycle_source=lifecycle_source,
    )

    # Projection (design §6): in-flight → running/0; completed → success/0;
    # failed/canceled/interrupted → failure/1. Status never writes the run dir.
    if effective_life in ("created", "running", "finalizing"):
        response = {
            "storedEnvelope": stored,
            "events": safe_events,
            "eventWarnings": event_warnings,
            "target": target,
            "summary": (
                "Run is still in progress. "
                "lifecycle={!r} ({}), process={!r}, events={}, elapsedMs={!r}."
            ).format(
                effective_life,
                lifecycle_source,
                process_liveness,
                len(safe_events),
                target.get("elapsedMs"),
            ),
        }
        safe_response = envelope_mod.redact_secret_material(response)
        return envelope_mod.build_envelope(
            run_id=run_id,
            mode="status",
            status="running",
            progressStreamPath=str(progress_path),
            warnings=warnings,
            response=safe_response,
        )

    if effective_life == "completed":
        response = {
            "storedEnvelope": stored,
            "events": safe_events,
            "eventWarnings": event_warnings,
            "target": target,
        }
        safe_response = envelope_mod.redact_secret_material(response)
        return envelope_mod.build_envelope(
            run_id=run_id,
            mode="status",
            status="success",
            progressStreamPath=str(progress_path),
            warnings=warnings,
            response=safe_response,
        )

    # failed / canceled / interrupted
    if stored is None and effective_life == "interrupted":
        warnings.append(
            "run {} has no stored envelope (recordStatus={!r}, process={!r}); "
            "run appears interrupted (derived; not persisted)".format(
                run_id, record_status, process_liveness
            )
        )
    response = {
        "storedEnvelope": stored,
        "events": safe_events,
        "eventWarnings": event_warnings,
        "target": target,
        "summary": "Target lifecycle={!r} (source={}).".format(effective_life, lifecycle_source),
    }
    safe_response = envelope_mod.redact_secret_material(response)
    return envelope_mod.build_envelope(
        run_id=run_id,
        mode="status",
        status="failure",
        progressStreamPath=str(progress_path),
        warnings=warnings,
        response=safe_response,
    )
