# wrapper/scripts/groklib/run_lifecycle.py
#
# Durable run lifecycle: seed/CAS/set_lifecycle, envelope-first terminal persist,
# and read-only effective_lifecycle projection (design §§6–7). Split from
# runstate.py to keep the state-layout broker under the 900-line file cap
# (AGENTS.md). Import-isolated: stdlib + groklib errors + runstate primitives.

from __future__ import annotations

import json
from typing import Dict, Optional, Set

from groklib import GrokWrapperError
from groklib import runstate

class CasConflictError(GrokWrapperError):
    """Raised when a CAS update observes an unexpected recordRevision."""

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("state-ownership-violation", message, detail)


class LifecycleError(GrokWrapperError):
    """Raised when a lifecycle transition is illegal or would overwrite a terminal state."""

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("state-ownership-violation", message, detail)


_TERMINAL_LIFECYCLES: Set[str] = frozenset({"completed", "failed", "canceled"})
# set_lifecycle: non-terminal edges only (terminal only via persist_terminal_envelope)
_SET_LIFECYCLE_TRANSITIONS: Dict[str, Set[str]] = {
    "created": frozenset({"running"}),
    "running": frozenset({"finalizing"}),
}
# New envelope write: completed only from finalizing; failed/canceled from earlier stages
_PERSIST_LIFECYCLE_TRANSITIONS: Dict[str, Set[str]] = {
    "created": frozenset({"failed", "canceled"}),
    "running": frozenset({"failed", "canceled"}),
    "finalizing": frozenset({"completed", "failed", "canceled"}),
}
_CAS_ALLOWED_KEYS: Set[str] = frozenset(
    {
        "schemaVersion",
        "mode",
        "requestedModel",
        "repository",
        "targetWorkspace",
        "worktreePath",
        "worktreeBranch",
        "baseRevision",
        "status",
        "progressStreamPath",
        "envelopePath",
    }
)
_PRESERVE_ON_MERGE: Set[str] = frozenset({"runId", "createdAtUtc", "lifecycle", "recordRevision"})



def seed_run_record(paths: "runstate.RunPaths", mode: str) -> dict:
    """Return the exact seed ``run.json`` body (schemaVersion 1, lifecycle created, revision 0)."""
    return {
        "schemaVersion": 1,
        "runId": paths.run_id,
        "mode": mode,
        "createdAtUtc": runstate._utc_now_iso(),
        "lifecycle": "created",
        "status": "running",
        "recordRevision": 0,
        "requestedModel": None,
        "repository": None,
        "targetWorkspace": None,
        "worktreePath": None,
        "worktreeBranch": None,
        "baseRevision": None,
        "progressStreamPath": str(paths.progress_path),
        "envelopePath": str(paths.envelope_path),
    }


def cas_update_run_record(
    paths: "runstate.RunPaths",
    expected_revision: int,
    patch: Dict[str, object],
) -> dict:
    """CAS merge ``patch`` into run.json under lock; bumps recordRevision by 1."""
    runstate._verify_paths_owner(paths)
    with runstate.run_lock(paths):
        record = runstate._load_run_json_unlocked(paths)
        current = record.get("recordRevision", 0)
        if current != expected_revision:
            raise CasConflictError(
                "recordRevision conflict for run {}: expected {}, found {}".format(
                    paths.run_id, expected_revision, current
                ),
                {
                    "runId": paths.run_id,
                    "expectedRevision": expected_revision,
                    "foundRevision": current,
                },
            )
        if "lifecycle" in patch:
            raise LifecycleError(
                "use set_lifecycle or persist_terminal_envelope to change lifecycle",
                {"runId": paths.run_id},
            )
        if record.get("lifecycle") in _TERMINAL_LIFECYCLES:
            raise LifecycleError(
                "refusing to mutate a terminal run record via cas_update_run_record",
                {"runId": paths.run_id, "lifecycle": record.get("lifecycle")},
            )
        unknown = set(patch.keys()) - _CAS_ALLOWED_KEYS
        if unknown:
            raise LifecycleError(
                "unknown run record fields: {}".format(sorted(unknown)),
                {"unknownFields": sorted(unknown)},
            )
        merged = dict(record)
        for key, value in patch.items():
            if key in ("runId", "createdAtUtc", "recordRevision"):
                continue
            merged[key] = value
        # Non-terminal: never store success/failure status without envelope
        if merged.get("status") in ("success", "failure"):
            merged["status"] = "running"
        merged["runId"] = paths.run_id
        merged["createdAtUtc"] = record.get("createdAtUtc") or merged.get("createdAtUtc")
        merged["recordRevision"] = expected_revision + 1
        runstate.write_json_atomic(paths.run_dir / "run.json", merged)
        return merged


def set_lifecycle(paths: "runstate.RunPaths", expected_revision: int, lifecycle: str) -> dict:
    """CAS non-terminal lifecycle transition under lock (design §6).

    Terminal lifecycles are **only** set by ``persist_terminal_envelope``.
    """
    runstate._verify_paths_owner(paths)
    if lifecycle in _TERMINAL_LIFECYCLES:
        raise LifecycleError(
            "set_lifecycle cannot set terminal lifecycle {!r}; use persist_terminal_envelope".format(
                lifecycle
            ),
            {"lifecycle": lifecycle, "runId": paths.run_id},
        )
    with runstate.run_lock(paths):
        record = runstate._load_run_json_unlocked(paths)
        current_rev = record.get("recordRevision", 0)
        if current_rev != expected_revision:
            raise CasConflictError(
                "recordRevision conflict for run {}: expected {}, found {}".format(
                    paths.run_id, expected_revision, current_rev
                ),
                {
                    "runId": paths.run_id,
                    "expectedRevision": expected_revision,
                    "foundRevision": current_rev,
                },
            )
        current_life = record.get("lifecycle")
        if current_life in _TERMINAL_LIFECYCLES:
            raise LifecycleError(
                "refusing to overwrite terminal lifecycle {!r}".format(current_life),
                {"runId": paths.run_id, "lifecycle": current_life},
            )
        allowed = _SET_LIFECYCLE_TRANSITIONS.get(str(current_life), frozenset())
        if lifecycle not in allowed:
            raise LifecycleError(
                "illegal lifecycle transition {!r} -> {!r}".format(current_life, lifecycle),
                {"from": current_life, "to": lifecycle, "runId": paths.run_id},
            )
        record = dict(record)
        record["lifecycle"] = lifecycle
        record["status"] = "running"
        record["recordRevision"] = expected_revision + 1
        runstate.write_json_atomic(paths.run_dir / "run.json", record)
        return record


def _lifecycle_from_envelope(envelope: dict) -> str:
    status = envelope.get("status")
    if status == "success":
        return "completed"
    if status == "failure":
        return "failed"
    raise LifecycleError(
        "cannot derive terminal lifecycle from envelope status {!r}".format(status),
        {"status": status},
    )


def _envelope_matches_lifecycle(envelope: dict, lifecycle: str) -> bool:
    status = envelope.get("status")
    if lifecycle == "completed":
        return status == "success"
    if lifecycle in ("failed", "canceled"):
        return status == "failure"
    return False


def _terminal_pair_compatible(lifecycle: str, envelope_status: str) -> bool:
    if lifecycle == "completed" and envelope_status == "success":
        return True
    if lifecycle == "failed" and envelope_status == "failure":
        return True
    if lifecycle == "canceled" and envelope_status == "failure":
        return True
    return False


def persist_terminal_envelope(
    paths: "runstate.RunPaths",
    expected_revision: Optional[int],
    envelope: Optional[dict],
    *,
    lifecycle: Optional[str] = None,
) -> dict:
    """Envelope-first terminal persistence under lock (design §7.1).

    If a valid terminal envelope already exists, finish lifecycle only (never
    replace the body). Otherwise require ``expected_revision`` + ``lifecycle``,
    write ``envelope.json`` first, then CAS lifecycle.
    """
    from groklib import envelope as envelope_mod

    runstate._verify_paths_owner(paths)
    with runstate.run_lock(paths):
        record = runstate._load_run_json_unlocked(paths)
        current_rev = int(record.get("recordRevision", 0))
        envelope_path = paths.envelope_path
        existing_env: Optional[dict] = None
        if envelope_path.is_file():
            try:
                with open(envelope_path, "r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
                if isinstance(candidate, dict) and not envelope_mod.validate_envelope(candidate):
                    existing_env = candidate
            except (OSError, json.JSONDecodeError):
                existing_env = None

        if existing_env is not None:
            implied = _lifecycle_from_envelope(existing_env)
            # Prefer canceled recovery if already recorded
            current_life = record.get("lifecycle")
            env_status = existing_env.get("status")
            if current_life in _TERMINAL_LIFECYCLES:
                if not _terminal_pair_compatible(str(current_life), str(env_status)):
                    raise LifecycleError(
                        "terminal lifecycle {!r} conflicts with existing envelope status {!r}".format(
                            current_life, env_status
                        ),
                        {"lifecycle": current_life, "envelopeStatus": env_status},
                    )
                return record
            # Finish lifecycle only (recovery): success→completed, failure→failed
            # (canceled not recoverable from envelope alone → failed)
            finish = implied
            record = dict(record)
            record["lifecycle"] = finish
            record["status"] = "success" if finish == "completed" else "failure"
            record["recordRevision"] = current_rev + 1
            runstate.write_json_atomic(paths.run_dir / "run.json", record)
            return record

        # New envelope path: refuse if already terminal without a valid envelope
        current_life = record.get("lifecycle")
        if current_life in _TERMINAL_LIFECYCLES:
            raise LifecycleError(
                "refusing new terminal envelope when lifecycle is already {!r}".format(current_life),
                {"lifecycle": current_life, "runId": paths.run_id},
            )
        if envelope is None:
            raise LifecycleError(
                "no terminal envelope on disk and none provided to persist",
                {"runId": paths.run_id},
            )
        if expected_revision is None:
            raise LifecycleError(
                "expected_revision is required when writing a new terminal envelope",
                {"runId": paths.run_id},
            )
        if current_rev != expected_revision:
            raise CasConflictError(
                "recordRevision conflict for run {}: expected {}, found {}".format(
                    paths.run_id, expected_revision, current_rev
                ),
                {
                    "runId": paths.run_id,
                    "expectedRevision": expected_revision,
                    "foundRevision": current_rev,
                },
            )
        if lifecycle is None:
            raise LifecycleError(
                "lifecycle is required when writing a new terminal envelope",
                {"runId": paths.run_id},
            )
        if lifecycle not in _TERMINAL_LIFECYCLES:
            raise LifecycleError(
                "lifecycle must be completed|failed|canceled, got {!r}".format(lifecycle),
                {"lifecycle": lifecycle},
            )
        if not _envelope_matches_lifecycle(envelope, lifecycle):
            raise LifecycleError(
                "lifecycle {!r} does not match envelope status {!r}".format(
                    lifecycle, envelope.get("status")
                ),
                {"lifecycle": lifecycle, "envelopeStatus": envelope.get("status")},
            )
        allowed = _PERSIST_LIFECYCLE_TRANSITIONS.get(str(current_life), frozenset())
        if lifecycle not in allowed:
            raise LifecycleError(
                "illegal terminal transition {!r} -> {!r}".format(current_life, lifecycle),
                {"from": current_life, "to": lifecycle, "runId": paths.run_id},
            )
        violations = envelope_mod.validate_envelope(envelope)
        if violations:
            raise envelope_mod.InvalidEnvelopeError(
                "terminal envelope failed validation",
                {"violations": violations},
            )
        # Envelope FIRST, then lifecycle
        runstate.write_json_atomic(envelope_path, envelope)
        record = dict(record)
        record["lifecycle"] = lifecycle
        record["status"] = "success" if lifecycle == "completed" else "failure"
        record["recordRevision"] = current_rev + 1
        runstate.write_json_atomic(paths.run_dir / "run.json", record)
        return record


def effective_lifecycle(
    record: dict,
    *,
    has_valid_envelope: bool,
    envelope_status: Optional[str],
    process_liveness: str,
) -> tuple:
    """Return ``(lifecycle, lifecycleSource)`` without writing (design §6)."""
    life = record.get("lifecycle")
    if life in _TERMINAL_LIFECYCLES:
        return str(life), "record"
    if has_valid_envelope:
        if envelope_status == "success":
            return "completed", "envelope"
        return "failed", "envelope"
    # Non-terminal lifecycle (or missing): never promote to completed without envelope
    if process_liveness == "dead":
        return "interrupted", "derived"
    if life in ("created", "running", "finalizing"):
        return str(life), "record"
    status = record.get("status")
    if status == "running":
        return "running", "record"
    return "running", "record"


