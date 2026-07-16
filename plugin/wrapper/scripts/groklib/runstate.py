# wrapper/scripts/groklib/runstate.py
#
# Broker seed module: owns run-id minting, the C2 on-disk state layout,
# owner-marker enforcement, leader-socket path allocation, and stale
# private-home cleanup. Import-isolated per spec section 4.1 item 5 so it
# can be reused (and unit-tested) without pulling in argparse, the mode
# handlers, the Grok CLI invoker, the sandbox, the rules loader, or the
# envelope builder. Allowed imports are stdlib only, plus the base
# GrokWrapperError and the shared log_stderr helper from the top-level
# groklib package.

import contextlib
import dataclasses
import datetime
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import tempfile
import time
from typing import Callable, Dict, List, Optional, Set

from groklib import GrokWrapperError, log_stderr
from groklib import platformsupport

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore

_OWNER_STRING = "grok-skills-wrapper"
_STATE_DIR_NAME = "grok-skills"
# F-RELAY-RUNID: the stable, machine-readable stderr line the plugin's foreground
# progress relay parses to follow the EXACT run this process created (instead of
# racily diffing the runs/ directory). Additive, stderr-only, safety-neutral;
# stdout stays the envelope's alone. progress-relay.mjs mirrors this prefix.
RUN_ID_STDERR_MARKER = "[grok-run-id]"
_RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
# Single source of truth for the private per-run Grok HOME directory name
# prefix (C2). authhome.py imports this constant for tempfile.mkdtemp and the
# stale-home audit below filters by it, so the two stay consistent
# automatically. The prefix is deliberately short ("gs-"): it is load-bearing
# for the AF_UNIX limit, keeping a realistic macOS $TMPDIR leader-socket path
# well under the 100-byte guard (a long prefix pushed it to 113 bytes, over the
# ~104 limit, and tripped allocate_leader_socket on every real macOS run).
TEMP_HOME_PREFIX = "gs-"
# Liveness lease file written into every private Grok home at creation (authhome
# imports this name). It holds the owning wrapper process's pid; the stale-home
# reaper reads it and NEVER removes a home whose owner pid is still alive, so a
# long-running live run (e.g. --timeout beyond the reap window) can never have
# its active credential-bearing home reaped out from under it (Grok dogfood-3 #1).
# A private home's mtime is stamped at creation and never refreshed, so age alone
# is not a live lease; the pid is.
TEMP_HOME_LIVENESS_FILENAME = "owner.pid"
_LEADER_SOCKET_MAX_BYTES = 100
_DIR_MODE = 0o700
_FILE_MODE = 0o600


class StateOwnershipError(GrokWrapperError):
    """Raised when an owner.json marker is missing, malformed, or names a different owner."""

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("state-ownership-violation", message, detail)


class UnknownRunError(GrokWrapperError):
    """Raised when a run id is malformed or has no matching run record on disk."""

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("invalid-target", message, detail)


class LeaderSocketPathTooLong(GrokWrapperError):
    """Raised when an allocated leader-socket path would exceed the AF_UNIX byte guard."""

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__("leader-socket-failure", message, detail)


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


@dataclasses.dataclass(frozen=True)
class RunPaths:
    run_id: str
    run_dir: pathlib.Path
    progress_path: pathlib.Path
    envelope_path: pathlib.Path
    trace_dir: pathlib.Path


def _log_stderr(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "runstate" component prefix."""
    log_stderr("runstate", function, message)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mkdir_0700(path: pathlib.Path) -> None:
    """Create ``path`` and any missing parents, forcing every newly created directory to exactly 0700.

    ``pathlib.Path.mkdir(parents=True, mode=...)`` only applies ``mode`` to the
    final path component and ignores it (and the umask) for intermediate
    parents, so each missing directory is created and hardened individually
    (via the platformsupport abstraction) to satisfy the C2 "all directories
    created with mode 0700" rule on POSIX and its ACL equivalent elsewhere.
    """
    to_create: List[pathlib.Path] = []
    current = path
    while not current.exists():
        to_create.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for directory in reversed(to_create):
        try:
            directory.mkdir(mode=_DIR_MODE, exist_ok=True)
            platformsupport.restrict_dir_permissions(directory)
        except OSError as exc:
            _log_stderr("_mkdir_0700", "failed to create {}: {}".format(directory, exc))
            raise


def _write_json_0600(path: pathlib.Path, payload: object) -> None:
    """Write ``payload`` as JSON to ``path``, forcing the file to exactly 0600 regardless of umask."""
    write_json_atomic(path, payload)


def write_json_atomic(path: pathlib.Path, payload: object) -> None:
    """Atomically write JSON: temp sibling ``path.name + '.tmp.' + pid`` then ``os.replace``, mode 0600."""
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    parent = path.parent
    tmp_name = "{}.tmp.{}".format(path.name, os.getpid())
    tmp_path = parent / tmp_name
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        platformsupport.restrict_file_permissions(tmp_path)
        os.replace(str(tmp_path), str(path))
        platformsupport.restrict_file_permissions(path)
    except OSError as exc:
        _log_stderr("write_json_atomic", "failed writing {}: {}".format(path, exc))
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


@contextlib.contextmanager
def run_lock(paths: RunPaths):
    """Exclusive lock on ``run_dir/run.lock`` (fcntl on Unix, msvcrt on Windows)."""
    lock_path = paths.run_dir / "run.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, _FILE_MODE)
    try:
        platformsupport.restrict_file_permissions(lock_path)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)


def _load_run_json_unlocked(paths: RunPaths) -> dict:
    record_path = paths.run_dir / "run.json"
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise UnknownRunError(
            "no run record for run id {}".format(paths.run_id),
            {"runId": paths.run_id},
        ) from exc
    if not isinstance(record, dict):
        raise UnknownRunError(
            "run record for run id {} is not a JSON object".format(paths.run_id),
            {"runId": paths.run_id},
        )
    return record


def _verify_paths_owner(paths: RunPaths) -> None:
    marker_path = paths.run_dir / "owner.json"
    owner_run_id = verify_owner_marker(marker_path)
    if owner_run_id != paths.run_id:
        raise StateOwnershipError(
            "owner marker run id mismatch for {}".format(paths.run_dir),
            {"expectedRunId": paths.run_id, "markerRunId": owner_run_id},
        )


def seed_run_record(paths: RunPaths, mode: str) -> dict:
    """Return the exact seed ``run.json`` body (schemaVersion 1, lifecycle created, revision 0)."""
    return {
        "schemaVersion": 1,
        "runId": paths.run_id,
        "mode": mode,
        "createdAtUtc": _utc_now_iso(),
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
    paths: RunPaths,
    expected_revision: int,
    patch: Dict[str, object],
) -> dict:
    """CAS merge ``patch`` into run.json under lock; bumps recordRevision by 1."""
    _verify_paths_owner(paths)
    with run_lock(paths):
        record = _load_run_json_unlocked(paths)
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
        write_json_atomic(paths.run_dir / "run.json", merged)
        return merged


def set_lifecycle(paths: RunPaths, expected_revision: int, lifecycle: str) -> dict:
    """CAS non-terminal lifecycle transition under lock (design §6).

    Terminal lifecycles are **only** set by ``persist_terminal_envelope``.
    """
    _verify_paths_owner(paths)
    if lifecycle in _TERMINAL_LIFECYCLES:
        raise LifecycleError(
            "set_lifecycle cannot set terminal lifecycle {!r}; use persist_terminal_envelope".format(
                lifecycle
            ),
            {"lifecycle": lifecycle, "runId": paths.run_id},
        )
    with run_lock(paths):
        record = _load_run_json_unlocked(paths)
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
        write_json_atomic(paths.run_dir / "run.json", record)
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
    paths: RunPaths,
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

    _verify_paths_owner(paths)
    with run_lock(paths):
        record = _load_run_json_unlocked(paths)
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
            write_json_atomic(paths.run_dir / "run.json", record)
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
        write_json_atomic(envelope_path, envelope)
        record = dict(record)
        record["lifecycle"] = lifecycle
        record["status"] = "success" if lifecycle == "completed" else "failure"
        record["recordRevision"] = current_rev + 1
        write_json_atomic(paths.run_dir / "run.json", record)
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


def emit_run_id_marker(run_id: str) -> None:
    """Write the run id to stderr as a stable machine-readable line for the plugin relay.

    Additive, stderr-only, safety-neutral (F-RELAY-RUNID). Emitted once, right
    after the run directory and its owner marker exist, so the foreground relay
    can bind to the EXACT run this process created rather than dir-diffing.
    Failure to write is logged and swallowed: a missing progress marker must
    never abort a real run.
    """
    try:
        os.write(2, "{} {}\n".format(RUN_ID_STDERR_MARKER, run_id).encode("utf-8"))
    except OSError as exc:
        _log_stderr("emit_run_id_marker", "could not write run-id marker for {}: {}".format(run_id, exc))


def state_root() -> pathlib.Path:
    # F-STATE-WS: a whitespace-only XDG_STATE_HOME is treated as UNSET (it would
    # otherwise yield base=Path("   ")); strip and fall back to the default.
    # F-STATE-ABS: per the XDG Base Directory spec, a RELATIVE XDG_STATE_HOME is
    # invalid and MUST be ignored. Honoring one would resolve run state (owner
    # marker, run.json, progress.jsonl, envelope.json -- which hold prompts and
    # model output) relative to the process CWD, typically an active git
    # worktree, at risk of being committed or scanned by other tooling. Reject
    # any non-absolute value and fall back to the default the same way.
    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state_home and pathlib.Path(xdg_state_home).is_absolute():
        base = pathlib.Path(xdg_state_home)
    else:
        base = pathlib.Path.home() / ".local" / "state"
    return base / _STATE_DIR_NAME


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)


def is_valid_run_id(candidate: str) -> bool:
    if not isinstance(candidate, str):
        return False
    return bool(_RUN_ID_PATTERN.match(candidate))


def _run_paths_for(run_id: str) -> RunPaths:
    run_dir = state_root() / "runs" / run_id
    return RunPaths(
        run_id=run_id,
        run_dir=run_dir,
        progress_path=run_dir / "progress.jsonl",
        envelope_path=run_dir / "envelope.json",
        trace_dir=run_dir / "trace",
    )


def create_run(mode: str) -> RunPaths:
    """Mint a fresh run id, seed ``run.json``, then emit the run-id marker.

    Seed is written **before** ``emit_run_id_marker`` so a published run id always
    has durable state (lifecycle ``created``, status ``running``, recordRevision 0).
    """
    run_id = new_run_id()
    paths = _run_paths_for(run_id)
    _mkdir_0700(paths.run_dir.parent)
    try:
        os.mkdir(str(paths.run_dir), _DIR_MODE)
    except FileExistsError as exc:
        _log_stderr(
            "create_run",
            "refusing to adopt existing run directory {} for run id {}".format(paths.run_dir, run_id),
        )
        raise StateOwnershipError(
            "run directory already exists for run id {}; an existing run directory is never adopted".format(
                run_id
            ),
            {"runId": run_id, "operation": "create_run"},
        ) from exc
    try:
        platformsupport.restrict_dir_permissions(paths.run_dir)
        write_owner_marker(paths.run_dir, run_id)
        write_home_liveness_marker(paths.run_dir, os.getpid())
        _mkdir_0700(paths.trace_dir)
        seed = seed_run_record(paths, mode)
        write_json_atomic(paths.run_dir / "run.json", seed)
        emit_run_id_marker(run_id)
    except BaseException as exc:
        _attach_run_paths(exc, paths)
        raise
    return paths


def _attach_run_paths(exc: BaseException, paths: RunPaths) -> None:
    """Attach ``paths`` to ``exc`` so a create_run partial failure terminalizes the REAL run.

    A best-effort side channel: the runners read ``getattr(exc, "run_paths", None)``
    to recover the real run id/dir when create_run raised after the run directory
    (and its owner marker) already existed, so the run is terminalized -- not
    orphaned under a synthesized id. Setting the attribute is guarded so a
    (pathological) exception type that forbids attribute assignment never masks
    the original failure.
    """
    try:
        exc.run_paths = paths  # type: ignore[attr-defined]
    except (AttributeError, TypeError) as attach_exc:
        _log_stderr("_attach_run_paths", "could not attach run paths to {}: {}".format(type(exc).__name__, attach_exc))


def write_owner_marker(directory: pathlib.Path, run_id: str) -> None:
    """Write ``directory/owner.json`` per the exact C2 shape, mode 0600.

    Reused both by ``create_run`` for the run directory and, in a later
    task, by the worktree module for sibling ``<path>.owner.json`` markers.
    Delegates to ``write_owner_marker_file`` so the C2 schema is constructed
    in exactly one place.
    """
    write_owner_marker_file(directory / "owner.json", run_id)


def write_owner_marker_file(marker_path: pathlib.Path, run_id: str) -> None:
    """Write the C2 owner marker schema to ``marker_path`` exactly, mode 0600.

    ``marker_path`` is the marker file itself (not a directory), so callers
    that need a marker at an arbitrary sibling path (Task 8's
    ``<worktree-path>.owner.json`` markers) can use this directly instead of
    going through ``write_owner_marker``'s ``directory/owner.json`` shape.
    """
    payload = {
        "schemaVersion": 1,
        "owner": _OWNER_STRING,
        "runId": run_id,
        "createdAtUtc": _utc_now_iso(),
    }
    _write_json_0600(marker_path, payload)


def verify_owner_marker(marker_path: pathlib.Path) -> str:
    """Verify the owner.json file at ``marker_path`` matches the C2 shape and owner string exactly.

    ``marker_path`` is the owner.json file itself (run dir: <run_dir>/owner.json;
    worktree: <path>.owner.json). Returns the marker's runId on success; raises
    StateOwnershipError (fail closed) on any missing, unreadable, malformed, or
    mismatched marker.
    """
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log_stderr("verify_owner_marker", "unreadable marker {}: {}".format(marker_path, exc))
        raise StateOwnershipError(
            "owner marker missing or unreadable: {}".format(marker_path),
            {"markerPath": str(marker_path)},
        ) from exc

    if not isinstance(payload, dict):
        _log_stderr("verify_owner_marker", "marker {} is not a JSON object".format(marker_path))
        raise StateOwnershipError(
            "owner marker is not a JSON object: {}".format(marker_path),
            {"markerPath": str(marker_path)},
        )

    schema_version = payload.get("schemaVersion")
    owner = payload.get("owner")
    run_id = payload.get("runId")
    created_at = payload.get("createdAtUtc")

    marker_is_valid = (
        schema_version == 1
        and owner == _OWNER_STRING
        and isinstance(run_id, str)
        and bool(run_id)
        and isinstance(created_at, str)
        and bool(created_at)
    )
    if not marker_is_valid:
        _log_stderr(
            "verify_owner_marker",
            "marker {} failed shape/owner check (owner={!r})".format(marker_path, owner),
        )
        raise StateOwnershipError(
            "owner marker shape or owner mismatch: {}".format(marker_path),
            {"markerPath": str(marker_path)},
        )

    return run_id


def allocate_leader_socket(private_home: pathlib.Path, run_id: str) -> pathlib.Path:
    """Return the leader-socket path under ``private_home`` for ``run_id``, guarding the AF_UNIX byte limit.

    macOS AF_UNIX paths are limited to 104 bytes; this guards to under 100
    bytes and raises LeaderSocketPathTooLong (fail closed) otherwise.

    The socket filename uses only the run-id's 6-hex tail
    (``run_id.rsplit("-", 1)[-1]``), not the full run id: the run id in the
    socket filename is NOT load-bearing (nothing parses it back out; the path
    is only ever passed to the CLI as ``--leader-socket <path>``), and per-run
    uniqueness is already guaranteed by the unique private home. The short name
    keeps a realistic macOS ``$TMPDIR`` socket path near ~79 bytes with margin.
    The length guard below stays as the fail-closed backstop regardless.
    """
    suffix = run_id.rsplit("-", 1)[-1]
    socket_path = private_home / ".grok" / "l-{}.sock".format(suffix)
    encoded_length = len(str(socket_path).encode("utf-8"))
    if encoded_length >= _LEADER_SOCKET_MAX_BYTES:
        _log_stderr(
            "allocate_leader_socket",
            "path {} is {} bytes, exceeds {}-byte guard".format(
                socket_path, encoded_length, _LEADER_SOCKET_MAX_BYTES
            ),
        )
        raise LeaderSocketPathTooLong(
            "leader socket path exceeds {} bytes: {}".format(_LEADER_SOCKET_MAX_BYTES, socket_path),
            {"path": str(socket_path), "bytes": encoded_length},
        )
    return socket_path


def write_run_record(paths: RunPaths, record: dict) -> None:
    """Legacy merge helper for non-terminal bookkeeping only.

    Prefer ``cas_update_run_record`` / ``set_lifecycle`` / ``persist_terminal_envelope``.
    Refuses to mutate terminal runs. Never sets terminal lifecycle. Never stores
    status success/failure while non-terminal.
    """
    _verify_paths_owner(paths)
    with run_lock(paths):
        existing: dict = {}
        record_path = paths.run_dir / "run.json"
        if record_path.is_file():
            try:
                existing = _load_run_json_unlocked(paths)
            except UnknownRunError:
                existing = {}
        if existing.get("lifecycle") in _TERMINAL_LIFECYCLES:
            raise LifecycleError(
                "refusing to mutate terminal run via write_run_record",
                {"runId": paths.run_id, "lifecycle": existing.get("lifecycle")},
            )
        merged = dict(existing)
        for key, value in record.items():
            if key in _PRESERVE_ON_MERGE and key in existing:
                continue
            if key == "lifecycle":
                continue
            if key not in _CAS_ALLOWED_KEYS and key not in ("schemaVersion", "mode"):
                continue  # drop unknown keys
            merged[key] = value
        merged["runId"] = paths.run_id
        if "createdAtUtc" not in merged or not merged["createdAtUtc"]:
            merged["createdAtUtc"] = existing.get("createdAtUtc") or _utc_now_iso()
        life = existing.get("lifecycle") or "created"
        if life == "created" and record.get("status") == "running":
            life = "running"
        merged["lifecycle"] = life
        status_value = record.get("status")
        if status_value in ("success", "failure", None):
            merged["status"] = "running"
        else:
            merged["status"] = status_value
        if existing:
            merged["recordRevision"] = int(existing.get("recordRevision", 0)) + 1
        else:
            merged["recordRevision"] = int(record.get("recordRevision", 0) or 0)
        write_json_atomic(record_path, merged)


def load_run_record(run_id: str) -> dict:
    """Load ``runs/<run_id>/run.json``. Raises UnknownRunError for malformed ids or missing records."""
    if not is_valid_run_id(run_id):
        _log_stderr("load_run_record", "rejected malformed run id {!r}".format(run_id))
        raise UnknownRunError("not a valid run id: {!r}".format(run_id), {"runId": run_id})

    record_path = state_root() / "runs" / run_id / "run.json"
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log_stderr("load_run_record", "failed to load {}: {}".format(record_path, exc))
        raise UnknownRunError("no run record for run id {}".format(run_id), {"runId": run_id}) from exc
    if not isinstance(record, dict):
        # A run.json that parses but is not a JSON object (e.g. "[]" or "oops")
        # is a corrupt/unusable record. Fail closed with the classified error
        # under the REQUESTED run id, so callers doing record.get(...) never hit a
        # later AttributeError that the entrypoint would then report under a
        # synthesized id.
        _log_stderr("load_run_record", "run.json for {} is a {} not an object".format(run_id, type(record).__name__))
        raise UnknownRunError(
            "run record for run id {} is not a JSON object".format(run_id),
            {"runId": run_id, "recordType": type(record).__name__},
        )
    return record


def is_orphaned_partial_run_dir(run_id: str) -> bool:
    """True when runs/<run_id>/ is reapable partial-create debris owned by the current user.

    Round5 unreapable-run-dir-on-create-run-partial-failure / Grok dogfood-3 #3: a
    create_run that fails AFTER ``os.mkdir`` but before/at the owner-marker write
    leaves a run directory with no valid owner.json and no run.json. The normal
    cleanup path (load_run_record -> verify_owner_marker) can never remove it, so
    it is stale state with no reaper. This predicate identifies EXACTLY that
    debris -- and never a foreign or an in-flight run's directory -- so cleanup
    can reap it: the dir must be a valid run-id name under our own runs/ root,
    owned by the current user (POSIX uid), have NO valid owner marker (a completed
    create_run always wrote one, so a valid marker means a real run), have NO
    loadable run.json, and (on POSIX) be a 0700 directory as create_run makes it.
    """
    if not is_valid_run_id(run_id):
        return False
    run_dir = state_root() / "runs" / run_id
    if not run_dir.is_dir():
        return False
    try:
        dir_stat = run_dir.stat()
    except OSError as exc:
        _log_stderr("is_orphaned_partial_run_dir", "cannot stat {}: {}".format(run_dir, exc))
        return False
    if platformsupport.is_posix() and stat.S_IMODE(dir_stat.st_mode) != _DIR_MODE:
        return False
    if not platformsupport.path_is_owned_by_current_user(dir_stat):
        return False
    # A run.json means the run advanced past create_run into its mode body; that is
    # a real run, never create_run debris.
    if (run_dir / "run.json").exists():
        return False
    # No run.json. Two reapable orphan shapes, both bound to the same liveness check
    # so a LIVE in-flight create is never reaped:
    #   (a) no valid owner marker: create_run crashed before/at the marker write.
    #       No lease exists yet either, so age/liveness cannot classify it -- this
    #       is the historical pre-marker debris signature and stays reapable.
    #   (b) valid owner marker but the owner process is PROVABLY DEAD: create_run
    #       wrote the marker+lease, the caller then crashed (SIGKILL/OOM/power loss)
    #       before writing run.json (F4-partial-create post-create crash window). A
    #       live or unknown owner is an in-flight create and must NEVER be reaped.
    try:
        verify_owner_marker(run_dir / "owner.json")
    except StateOwnershipError:
        return True  # (a) pre-marker debris
    return _home_owner_liveness(run_dir) == _LIVENESS_DEAD  # (b) dead post-create crash


def remove_partial_run_dir(run_id: str) -> None:
    """Remove a reapable partial-create run directory, re-checking ownership first (fail-closed).

    Only ever called by cleanup for a dir that ``is_orphaned_partial_run_dir``
    already vouched for; the predicate is re-evaluated here as a defense-in-depth
    guard so a foreign or in-flight dir can never be removed even if the caller is
    wrong. A removal failure raises cleanup-failure.
    """
    if not is_orphaned_partial_run_dir(run_id):
        raise StateOwnershipError(
            "refusing to remove {} as partial-create debris: it is not owned partial-create state".format(run_id),
            {"runId": run_id, "operation": "remove_partial_run_dir"},
        )
    run_dir = state_root() / "runs" / run_id
    failed_paths: List[str] = []

    def _on_error(_func: object, path: str, _exc_info: object) -> None:
        failed_paths.append(path)

    shutil.rmtree(str(run_dir), onerror=_on_error)
    if failed_paths:
        _log_stderr(
            "remove_partial_run_dir",
            "cleanup failed for {} path(s) under {}".format(len(failed_paths), run_dir),
        )
        raise GrokWrapperError(
            "cleanup-failure",
            "failed to remove partial-create run directory {}".format(run_dir),
            {"runDir": str(run_dir), "failedEntries": len(failed_paths)},
        )


def list_run_ids() -> List[str]:
    """List every valid run id under the state root, newest first by run-id lexical order."""
    runs_dir = state_root() / "runs"
    if not runs_dir.exists():
        return []

    try:
        entries = list(runs_dir.iterdir())
    except OSError as exc:
        _log_stderr("list_run_ids", "failed to list {}: {}".format(runs_dir, exc))
        raise GrokWrapperError(
            "cleanup-failure", "failed to list run directory {}".format(runs_dir), {"runsDir": str(runs_dir)}
        ) from exc

    run_ids = [entry.name for entry in entries if entry.is_dir() and is_valid_run_id(entry.name)]
    run_ids.sort(reverse=True)
    return run_ids


def write_home_liveness_marker(directory: pathlib.Path, pid: int) -> None:
    """Write the owning ``pid`` (plus its start-time identity token) into ``directory/owner.pid`` (0600).

    The private-home / run-dir live lease. Called by authhome.create_private_home
    the instant a home exists, and by create_run the instant a run dir exists.
    ``startToken`` binds the lease to the SPECIFIC process (F2/F4 pid-reuse): a
    recycled pid has a different start time, so a dead run's lease can never make
    an unrelated live process look like the still-running owner. A file write
    failure is logged and swallowed: a missing lease must never abort a real run.
    Its reaper consequence is FAIL-SAFE (Grok dogfood-4 #3): a missing/unreadable
    lease is treated as POSSIBLY-ACTIVE and never reaped, never age-only reaped.
    """
    start_token = platformsupport.process_start_token(pid)
    try:
        _write_json_0600(
            directory / TEMP_HOME_LIVENESS_FILENAME,
            {"schemaVersion": 1, "pid": pid, "startToken": start_token},
        )
    except OSError as exc:
        _log_stderr("write_home_liveness_marker", "could not write liveness marker for {}: {}".format(directory, exc))


# Tri-state owner-liveness of a home/run dir, from its owner.pid lease.
_LIVENESS_ALIVE = "alive"
_LIVENESS_DEAD = "dead"
_LIVENESS_UNKNOWN = "unknown"

# Grok r3 #11 / Grok r5 #5: the fail-closed upper bound on an operator-supplied
# --timeout. grok_agent enforces it at argv parse (its ceiling imports THIS
# constant so the argv clamp and the reaper share one source of truth). It is also
# the basis of the hard cap below: no run can outlast this budget.
MAX_RUN_TIMEOUT_SECONDS = 7 * 24 * 3600
# Grok r5 #5 unknown-lease-hard-cap: a home whose owner.pid lease could not be
# written (disk full, permission) reads as UNKNOWN forever and, under the pure
# fail-safe, would never be reaped -- stranding a copy of the operator's Grok auth
# material on disk indefinitely (fail-open for credential residue). A home OLDER
# than the maximum permitted --timeout PLUS a generous teardown margin cannot
# belong to any still-running run (the longest run allowed already exceeded its
# wall-clock budget and was killed), so past this cap an UNKNOWN-lease home is
# definitely-dead residue and IS reaped. Within the cap the fail-safe still holds:
# an unknown lease is treated as possibly-active and never age-reaped.
_UNKNOWN_LEASE_HARD_REAP_MARGIN_SECONDS = 6 * 3600
UNKNOWN_LEASE_HARD_REAP_AGE_SECONDS = MAX_RUN_TIMEOUT_SECONDS + _UNKNOWN_LEASE_HARD_REAP_MARGIN_SECONDS


def _home_owner_liveness(candidate: pathlib.Path) -> str:
    """Classify ``candidate``'s owner process as "alive", "dead", or "unknown" from its owner.pid lease.

    - "unknown": the lease is missing, unreadable, malformed, or carries no usable
      pid. Grok dogfood-4 #3 reaper-lease-fail-safe: this is POSSIBLY-ACTIVE and
      the reaper must NEVER remove it (never fall back to age-only reaping).
    - "dead": the lease is valid and the pid is gone, OR the pid is alive but its
      current start-time identity token DIFFERS from the stored one (F2/F4: the
      pid was recycled onto an unrelated process, so the original run IS dead).
    - "alive": the lease is valid, the pid is alive, and either the identity token
      matches or no token could be obtained to compare (conservative: an
      unverifiable live pid is treated as the still-running owner, never reaped).
    """
    marker_path = candidate / TEMP_HOME_LIVENESS_FILENAME
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log_stderr("_home_owner_liveness", "lease unreadable for {} (possibly-active): {}".format(candidate, exc))
        return _LIVENESS_UNKNOWN
    if not isinstance(payload, dict):
        _log_stderr("_home_owner_liveness", "lease for {} is not an object (possibly-active)".format(candidate))
        return _LIVENESS_UNKNOWN
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        _log_stderr("_home_owner_liveness", "lease for {} has no usable pid (possibly-active)".format(candidate))
        return _LIVENESS_UNKNOWN
    if not platformsupport.process_is_alive(pid):
        return _LIVENESS_DEAD
    stored_token = payload.get("startToken")
    current_token = platformsupport.process_start_token(pid)
    if isinstance(stored_token, str) and current_token is not None and stored_token != current_token:
        # The pid is alive but belongs to a DIFFERENT process now (pid reuse);
        # the run that wrote this lease is dead.
        _log_stderr("_home_owner_liveness", "pid {} recycled for {}; owner is dead".format(pid, candidate))
        return _LIVENESS_DEAD
    return _LIVENESS_ALIVE


def _is_removable_stale_home(candidate: pathlib.Path, now: float, max_age_seconds: int) -> bool:
    try:
        link_stat = os.lstat(str(candidate))
    except OSError as exc:
        _log_stderr("_is_removable_stale_home", "cannot lstat {}: {}".format(candidate, exc))
        return False
    if stat.S_ISLNK(link_stat.st_mode):
        # A symlink candidate is never removed: shutil.rmtree refuses to
        # operate on a symlink and would abort the whole audit (raising
        # cleanup-failure) if this candidate were allowed through to
        # _remove_stale_home. Skip it and let the audit continue.
        _log_stderr("_is_removable_stale_home", "skipping symlink candidate {}".format(candidate))
        return False

    # Grok dogfood-3 #1 + dogfood-4 #3: NEVER reap a home whose owner is ALIVE. A
    # home's mtime is stamped at creation and never refreshed, so a live run with a
    # --timeout longer than the reap window would otherwise look "old enough" and
    # have its active credential-bearing home deleted mid-run. This gate runs
    # BEFORE the age check so an active home is protected regardless of how old it
    # looks.
    #   - DEAD owner (pid gone, or recycled onto another process): reapable once
    #     older than the live-start window (``max_age_seconds``).
    #   - UNKNOWN owner (lease missing/unreadable -- its write may have failed):
    #     POSSIBLY-ACTIVE, so NOT reaped within the window (the fail-safe holds).
    #     But once older than UNKNOWN_LEASE_HARD_REAP_AGE_SECONDS (max --timeout +
    #     margin) it cannot belong to any live run, so it is reaped as
    #     definitely-dead credential residue rather than stranded forever (r5 #5).
    liveness = _home_owner_liveness(candidate)
    if liveness == _LIVENESS_ALIVE:
        _log_stderr("_is_removable_stale_home", "skipping {} (owner is alive)".format(candidate))
        return False
    reap_age_threshold = (
        max_age_seconds if liveness == _LIVENESS_DEAD else UNKNOWN_LEASE_HARD_REAP_AGE_SECONDS
    )

    try:
        dir_stat = candidate.stat()
    except OSError as exc:
        _log_stderr("_is_removable_stale_home", "cannot stat {}: {}".format(candidate, exc))
        return False

    # The 0700 mode gate is a POSIX octal check; on Windows directory mode
    # bits are not the protection mechanism (the per-user profile temp dir's
    # ACL is), so it is skipped there and ownership + owner marker + mtime
    # carry the safety guarantee. Ownership itself routes through the
    # platformsupport abstraction (POSIX st_uid == getuid; Windows best-effort
    # within the per-user temp root) instead of a raw os.getuid comparison.
    if platformsupport.is_posix() and stat.S_IMODE(dir_stat.st_mode) != _DIR_MODE:
        return False
    if not platformsupport.path_is_owned_by_current_user(dir_stat):
        return False
    if (now - dir_stat.st_mtime) < reap_age_threshold:
        _log_stderr(
            "_is_removable_stale_home",
            "skipping {} (age below {} threshold for {} lease)".format(
                candidate, reap_age_threshold, liveness
            ),
        )
        return False

    marker_path = candidate / "owner.json"
    try:
        verify_owner_marker(marker_path)
    except StateOwnershipError as exc:
        _log_stderr(
            "_is_removable_stale_home",
            "skipping {} (owner marker did not verify: {})".format(candidate, exc),
        )
        return False

    return True


def _remove_stale_home(candidate: pathlib.Path) -> None:
    failed_paths: List[str] = []

    def _on_error(_func: object, path: str, _exc_info: object) -> None:
        failed_paths.append(path)

    shutil.rmtree(str(candidate), onerror=_on_error)

    if failed_paths:
        _log_stderr(
            "_remove_stale_home",
            "cleanup failed for {} path(s) under {}".format(len(failed_paths), candidate),
        )
        raise GrokWrapperError(
            "cleanup-failure",
            "failed to remove stale temp home {}".format(candidate),
            {"path": str(candidate), "failedEntries": len(failed_paths)},
        )


def audit_stale_temp_homes(max_age_seconds: int) -> List[str]:
    """Remove expired, owned, correctly-permissioned private Grok homes under the OS temp dir.

    Scans ``tempfile.gettempdir()`` for ``TEMP_HOME_PREFIX + "*"`` entries and
    removes only those where the owner marker verifies, the directory is
    owned by the current user (via ``platformsupport.path_is_owned_by_current_user``:
    POSIX uid match; Windows best-effort within the per-user temp root), the
    mtime is older than ``max_age_seconds``, and (on POSIX) the directory mode
    is exactly 0700. Returns the removed paths.
    """
    temp_root = pathlib.Path(tempfile.gettempdir())
    now = time.time()
    removed: List[str] = []

    try:
        candidates = sorted(
            entry for entry in temp_root.iterdir() if entry.name.startswith(TEMP_HOME_PREFIX) and entry.is_dir()
        )
    except OSError as exc:
        _log_stderr("audit_stale_temp_homes", "failed to list {}: {}".format(temp_root, exc))
        raise GrokWrapperError(
            "cleanup-failure",
            "failed to list temp root {}: {}".format(temp_root, exc),
            {"tempRoot": str(temp_root)},
        ) from exc

    for candidate in candidates:
        if not _is_removable_stale_home(candidate, now, max_age_seconds):
            continue
        _remove_stale_home(candidate)
        removed.append(str(candidate))

    return removed


# Live-mode start reaping window (Grok dogfood-2 #1/#7). A crashed run (SIGKILL,
# OOM, power loss) can strand a gs-* private home holding a live copy of the
# operator's Grok auth material; previously only an occasional 24h preflight
# sweep reaped it. Live modes now reap on START with THIS shorter window. The
# PRIMARY protection against reaping an ACTIVE home is the owner-pid liveness
# lease (Grok dogfood-3 #1): _is_removable_stale_home never removes a home whose
# owner process is still alive, so a run with a --timeout LONGER than this window
# is safe -- age alone can never reap a live run's home. This age gate is the
# secondary guard for a genuinely dead run: only owner-marked, current-user-owned,
# 0700 homes whose owner pid is gone AND that are older than this window are
# removed. Foreign or unmarked dirs are never touched.
LIVE_START_STALE_HOME_MAX_AGE_SECONDS = 4 * 3600


def best_effort_reap_stale_temp_homes(max_age_seconds: int) -> List[str]:
    """Reap stale owner-marked private homes, swallowing failures so a run start never aborts.

    Wraps ``audit_stale_temp_homes`` for the live-mode start hygiene sweep: an
    opportunistic reap of a crashed run's leftover credential-bearing home must
    never block or fail the NEW run. A listing/removal failure is logged and the
    caller proceeds; the return value is the removed paths (empty on any failure).
    """
    try:
        return audit_stale_temp_homes(max_age_seconds)
    except GrokWrapperError as exc:
        _log_stderr(
            "best_effort_reap_stale_temp_homes",
            "stale private-home reap failed (continuing with the run): {}".format(exc),
        )
        return []
