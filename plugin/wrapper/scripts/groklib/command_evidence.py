# wrapper/scripts/groklib/command_evidence.py
#
# Bounded redacted command evidence for gates and contract requiredValidation.
# Single helper: sha256 of full streams + redacted tails (max 4096 bytes).

from __future__ import annotations

import hashlib
from typing import Any, Dict

from groklib.envelope import redact_secret_value_text

_MAX_TAIL = 4096


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tail_text(data: bytes, limit: int = _MAX_TAIL) -> Dict[str, Any]:
    """Redact the full stream first, then take the tail (never slice before redaction).

    Slicing raw output first can drop a secret marker (e.g. ``Bearer `` or PEM
    header) that sits just before the retained window, leaking the remainder.
    """
    raw = data.decode("utf-8", errors="replace")
    redacted = redact_secret_value_text(raw)
    truncated = len(redacted) > limit
    tail = redacted[-limit:] if truncated else redacted
    return {
        "text": tail,
        "truncated": truncated,
        "bytes": len(data),
    }


def build_command_evidence(
    *,
    argv: list,
    cwd: str,
    purpose: str,
    exit_status: int,
    stdout: bytes = b"",
    stderr: bytes = b"",
    duration_seconds: float = 0.0,
) -> dict:
    """Build a C4-compatible commands[] record with bounded redacted evidence.

    Always includes required envelope fields (argv, exitStatus, durationSeconds,
    purpose, cwd) plus optional evidence hashes/tails. Never adds unknown keys
    (detail belongs on blockers / error.detail, not commands[]).
    """
    safe_argv = [redact_secret_value_text(str(a)) for a in argv]
    return {
        "argv": safe_argv,
        "cwd": cwd,
        "purpose": purpose,
        "exitStatus": int(exit_status),
        "durationSeconds": float(duration_seconds),
        "stdoutSha256": _sha256_bytes(stdout or b""),
        "stderrSha256": _sha256_bytes(stderr or b""),
        "stdoutTail": _tail_text(stdout or b""),
        "stderrTail": _tail_text(stderr or b""),
    }
