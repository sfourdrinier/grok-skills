# wrapper/scripts/groklib/command_evidence.py
#
# Bounded redacted command evidence for gates and contract requiredValidation.
# Single helper: sha256 of full streams + redacted tails (max 4096 bytes).

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from groklib.envelope import redact_secret_value_text

_MAX_TAIL = 4096


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tail_text(data: bytes, limit: int = _MAX_TAIL) -> Dict[str, Any]:
    raw = data.decode("utf-8", errors="replace")
    truncated = len(raw) > limit
    tail = raw[-limit:] if truncated else raw
    return {
        "text": redact_secret_value_text(tail),
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
    detail: Optional[str] = None,
) -> dict:
    """Build a single command evidence record (never full logs on envelope stdout)."""
    rec: Dict[str, Any] = {
        "argv": list(argv),
        "cwd": cwd,
        "purpose": purpose,
        "exitStatus": int(exit_status),
        "stdoutSha256": _sha256_bytes(stdout or b""),
        "stderrSha256": _sha256_bytes(stderr or b""),
        "stdoutTail": _tail_text(stdout or b""),
        "stderrTail": _tail_text(stderr or b""),
    }
    if detail:
        rec["detail"] = redact_secret_value_text(str(detail))
    return rec
