# wrapper/scripts/groklib/progress.py
#
# C3 progress event stream: a strictly append-only, newline-delimited JSON
# ("JSONL") event log per run, written by ProgressWriter and consumed by
# read_events. Every event is appended with a single open("a") + write() +
# flush() so a concurrent reader (e.g. the `status` mode) never observes a
# torn line as valid JSON; a trailing partial line is skipped by the reader
# with a warning instead of raising.

import datetime
import json
import os
import pathlib
import time
from typing import Dict, List, Optional, Tuple

from groklib import log_stderr
from groklib import platformsupport
from groklib import injectedsecrets
from groklib.envelope import redact_secret_material, redact_secret_value_text

# progress.jsonl can quote secret material. Redact on write (patterns + injected
# denylist) before append; also keep 0600 owner-only mode.
_PROGRESS_FILE_MODE = 0o600

PHASES = (
    "start",
    "rules",
    "authhome",
    "sandbox",
    "worktree",
    "grok",
    "validate",
    "finalizing",
    "cleanup",
    "done",
)
LEVELS = ("info", "warning", "error")

_SCHEMA_VERSION = 1


class InvalidProgressEventError(ValueError):
    """Raised when an emit() call violates the C3 phase/level/data contract.

    A ``ValueError``, not a ``GrokWrapperError``: this guards module-owned
    constants (``PHASES``, ``LEVELS``, the data-must-be-dict-or-None rule)
    against programmer error, not an operator-facing classified failure. The
    entrypoint maps any escaped non-``GrokWrapperError`` exception to the
    ``cli-failure`` error class.
    """

    def __init__(self, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__(message)
        self.detail: Dict[str, object] = detail if detail is not None else {}


def _log_stderr(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "progress" component."""
    log_stderr("progress", function, message)


class ProgressWriter:
    """Appends C3 progress events to a single run's ``progress.jsonl`` file.

    One writer instance owns the strictly monotonic ``seq`` counter for the
    lifetime of a run; callers must not construct more than one writer per
    run against the same path, or the per-file monotonic-seq guarantee is
    broken.
    """

    def __init__(self, run_id: str, path: pathlib.Path) -> None:
        self.run_id: str = run_id
        self.path: pathlib.Path = path
        self._seq: int = 0
        # Process-local monotonic clock for elapsedMs (design §8); not shared across spawn.
        self._started_monotonic: float = time.monotonic()
        # F-STREAM-OSERR: flipped True the first time a progress.jsonl append
        # fails with OSError. Once degraded, every ``safe_emit`` is a no-op so a
        # persistent write failure (disk full, revoked permission) can neither
        # crash the real run nor spam the log.
        self._degraded: bool = False
        # Flipped True once the file's owner-only permissions have been asserted
        # (POSIX chmod / Windows ACL), so the cross-platform tightening runs once,
        # not on every append.
        self._permissions_secured: bool = False

    def safe_emit(
        self,
        phase: str,
        message: str,
        level: str = "info",
        data: Optional[Dict[str, object]] = None,
    ) -> Optional[Dict[str, object]]:
        """Emit one event, degrading (not raising) on a progress-write OSError.

        A progress.jsonl append failure MUST NEVER abort the real run: the
        run's own result, C4 envelope, and run-record (all written under the
        real run id) matter more than a live progress line. The FIRST OSError
        logs once and flips this writer into a degraded state; every subsequent
        call returns immediately without touching the stream. This is the single
        source of truth for the F-STREAM-OSERR degrade behavior, shared by the
        streaming relay and the whole run lifecycle.

        ``InvalidProgressEventError`` (a ValueError raised on a programmer
        contract violation -- a bad phase/level or non-serializable data -- not
        an I/O failure) is deliberately NOT swallowed: that is a code bug the
        caller must fix, not a runtime condition to degrade past.
        """
        if self._degraded:
            return None
        try:
            return self.emit(phase, message, level=level, data=data)
        except OSError as exc:
            self._degraded = True
            # F4: the degrade-path diagnostic must itself be failure-safe. The
            # primary degrade action (flipping self._degraded) already happened;
            # the diagnostic goes to stderr (log_stderr -> os.write(2, ...)), which
            # can ITSELF raise OSError when stderr is failing too -- a realistic
            # co-occurrence, since a redirected stderr log often shares the volume
            # that just failed the progress.jsonl write. safe_emit's contract is
            # that a progress-write failure never raises, so a second OSError here
            # is dropped: there is no remaining channel to report it on, and the
            # run continues (already degraded).
            try:
                _log_stderr(
                    "safe_emit",
                    "progress write failed; degrading (run continues, further progress dropped): {}".format(exc),
                )
            except OSError:
                pass
            return None

    def emit(
        self,
        phase: str,
        message: str,
        level: str = "info",
        data: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        """Append one C3 event and return the exact dict that was written.

        Raises InvalidProgressEventError (a ValueError) for any phase not in
        PHASES, any level not in LEVELS, any data value that is neither a
        dict nor None, or any event whose data is not JSON-serializable. No
        line is written to the stream, and the monotonic seq counter is not
        advanced, when validation or serialization fails.
        """
        if phase not in PHASES:
            raise InvalidProgressEventError(
                "invalid phase {!r}; must be one of {}".format(phase, PHASES),
                {"phase": phase, "allowedPhases": list(PHASES)},
            )
        if level not in LEVELS:
            raise InvalidProgressEventError(
                "invalid level {!r}; must be one of {}".format(level, LEVELS),
                {"level": level, "allowedLevels": list(LEVELS)},
            )
        if data is not None and not isinstance(data, dict):
            raise InvalidProgressEventError(
                "data must be a dict or None, got {}".format(type(data).__name__),
                {"dataType": type(data).__name__},
            )

        # Serialize against a candidate seq BEFORE committing self._seq, so an
        # unserializable data payload fails closed without consuming a seq
        # number: the strictly-monotonic C3 guarantee must never skip a
        # value just because a candidate event was rejected.
        candidate_seq = self._seq + 1
        safe_message = redact_secret_value_text(message if isinstance(message, str) else str(message))
        safe_message = injectedsecrets.redact_injected_secrets(safe_message)
        if not isinstance(safe_message, str):
            safe_message = str(safe_message)
        safe_data: Optional[dict] = None
        if data is not None:
            redacted = redact_secret_material(data)
            redacted = injectedsecrets.redact_injected_secrets(redacted)
            safe_data = redacted if isinstance(redacted, dict) else {"_redacted": True}
        elapsed_ms = int(max(0.0, (time.monotonic() - self._started_monotonic) * 1000.0))
        event: Dict[str, object] = {
            "schemaVersion": _SCHEMA_VERSION,
            "runId": self.run_id,
            "seq": candidate_seq,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "phase": phase,
            "level": level,
            "message": safe_message,
            "elapsedMs": elapsed_ms,
        }
        if safe_data is not None:
            event["data"] = safe_data

        try:
            line = json.dumps(event, sort_keys=True) + "\n"
        except TypeError as exc:
            _log_stderr(
                "emit",
                "failed serializing event seq={} phase={} for run {}: {}".format(
                    candidate_seq, phase, self.run_id, exc
                ),
            )
            raise InvalidProgressEventError(
                "event data is not JSON-serializable: {}".format(exc),
                {"phase": phase, "operation": "emit"},
            ) from exc

        try:
            # os.open with an explicit 0600 creation mode guarantees the file is
            # owner-only from the moment it is created (a plain open("a") would use
            # the umask-derived mode). O_APPEND keeps the strictly append-only
            # single-writer semantics. The mode argument only applies at creation;
            # subsequent appends to an existing file leave its perms untouched.
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, _PROGRESS_FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        except OSError as exc:
            # Do NOT advance self._seq on a failed write: a burned seq would skip a
            # value in the strictly-monotonic C3 stream. seq is committed only
            # after the append durably succeeds.
            _log_stderr("emit", "failed appending event seq={} to {}: {}".format(candidate_seq, self.path, exc))
            raise

        self._seq = candidate_seq

        if not self._permissions_secured:
            # Cross-platform owner-only tightening (Windows ACL as well as POSIX);
            # best-effort so a hardening hiccup never crashes a live run, but the
            # run dir is already 0700 so contents stay protected regardless.
            try:
                platformsupport.restrict_file_permissions(self.path)
                self._permissions_secured = True
            except OSError as exc:
                _log_stderr(
                    "emit", "could not restrict progress file permissions on {}: {}".format(self.path, exc)
                )

        return event


def read_events(path: pathlib.Path) -> Tuple[List[Dict[str, object]], List[str]]:
    """Read a C3 progress stream, returning (events, warnings).

    Torn or otherwise invalid lines (partial JSON from a concurrent
    in-flight append, a line that ends partway through a multibyte UTF-8
    sequence, non-JSON content, JSON that is not an object) are skipped and
    reported as a warning string per line; this function never raises for
    malformed line content. The file is read as raw bytes and split on
    ``b"\\n"`` so a torn trailing line that ends mid-multibyte-character
    cannot raise ``UnicodeDecodeError`` out of a whole-file strict-UTF-8
    decode; each line is decoded independently instead. A missing file
    returns an empty event list plus one warning. An unreadable-but-present
    file (permission error, I/O error) is logged to stderr with
    function/operation context and reported as a warning, also without
    raising.
    """
    events: List[Dict[str, object]] = []
    warnings: List[str] = []

    if not path.exists():
        warnings.append("progress stream not found: {}".format(path))
        return events, warnings

    try:
        with open(str(path), "rb") as handle:
            raw_bytes = handle.read()
    except OSError as exc:
        _log_stderr("read_events", "failed reading {}: {}".format(path, exc))
        warnings.append("failed to read progress stream {}: {}".format(path, exc))
        return events, warnings

    raw_lines = raw_bytes.split(b"\n")

    for line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            continue

        try:
            stripped = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            _log_stderr(
                "read_events", "skipping undecodable UTF-8 at line {} of {}: {}".format(line_number, path, exc)
            )
            warnings.append("skipped undecodable UTF-8 at line {} of {}: {}".format(line_number, path, exc))
            continue

        try:
            candidate = json.loads(stripped)
        except json.JSONDecodeError as exc:
            _log_stderr("read_events", "skipping invalid JSON at line {} of {}: {}".format(line_number, path, exc))
            warnings.append("skipped invalid JSON at line {} of {}: {}".format(line_number, path, exc))
            continue

        if not isinstance(candidate, dict):
            _log_stderr(
                "read_events",
                "skipping non-object JSON at line {} of {} (type {})".format(
                    line_number, path, type(candidate).__name__
                ),
            )
            warnings.append("skipped non-object JSON at line {} of {}".format(line_number, path))
            continue

        events.append(candidate)

    return events, warnings
