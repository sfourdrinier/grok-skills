# wrapper/scripts/tests/live/_probe_common.py
#
# Shared infrastructure for the Task 13/14 live probe modules (live_probes.py's
# read-only suite and code_verify_probe.py's write-capable code/verify handoff
# probe). Split out so no single probe file crosses the 900-line cap; this
# module holds ONLY the pieces both suites depend on: path anchors, the
# wrapper-owned verdict schema, the redaction-safe ProbeResult/ProbeError types,
# the envelope-driving `_run_wrapper` helper, and the envelope highlight /
# progress readers. It never imports another local probe module, so the split is
# acyclic. Python stdlib only; 3.9 syntax. No em/en dashes. No empty excepts.

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

# tests/live/_probe_common.py -> parents[2] is scripts/, parents[3] is grok-cli/.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2]
_SKILL_ROOT = _SCRIPTS_DIR.parent
_WRAPPER = _SCRIPTS_DIR / "grok_agent.py"
_ACCEPTED_VERSION_FILE = _SKILL_ROOT / "accepted-version.json"
_GROK_BINARY = pathlib.Path(os.path.expanduser(os.path.join("~", ".grok", "bin", "grok")))
_SOURCE_AUTH = pathlib.Path.home() / ".grok" / "auth.json"

_VERSION_PATTERN = re.compile(r"\d+\.\d+")

# The wrapper-owned verify verdict schema (Task 11 modes/verify.py). Reused by
# the read-only structured probe and the verify handoff probe so both exercise
# the exact shape the verify mode relies on live.
_VERDICT_SCHEMA: Dict[str, object] = {
    "type": "object",
    "required": ["verdict", "evidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def _utc_now_iso_z() -> str:
    """UTC timestamp as ...Z (matching the accepted-version.json validatedAtUtc shape)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _installed_version_first_line() -> str:
    """First line of `grok --version`, the exact string the C6 pin compares against."""
    completed = subprocess.run(
        [str(_GROK_BINARY), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("grok --version exited {}".format(completed.returncode))
    lines = (completed.stdout or "").splitlines()
    if not lines:
        raise RuntimeError("grok --version produced no output")
    return lines[0].strip()


class ProbeResult:
    """One probe's outcome: name, gating flag, pass/fail, command, and redacted highlights."""

    def __init__(
        self,
        name: str,
        gating: bool,
        passed: bool,
        command: str,
        highlights: Dict[str, object],
        detail: str,
    ) -> None:
        self.name = name
        self.gating = gating
        self.passed = passed
        self.command = command
        self.highlights = highlights
        self.detail = detail

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "gating": self.gating,
            "passed": self.passed,
            "command": self.command,
            "highlights": self.highlights,
            "detail": self.detail,
        }


class ProbeError(Exception):
    """A probe assertion failed. The message is the human-readable reason."""


def _run_wrapper(mode_args: List[str], timeout: int) -> Tuple[int, Dict[str, object], str, float]:
    """Run `python3 grok_agent.py <mode_args>` against the REAL binary; return (exit, envelope, stderr, seconds).

    The real environment is inherited (so HOME resolves ~/.grok and the default
    ~/.grok/bin/grok binary), with GROK_AGENT_BINARY explicitly removed so the
    wrapper never picks up a fake test binary from an ambient env. stdout must be
    exactly one JSON envelope; anything else is a probe failure.
    """
    env = dict(os.environ)
    env.pop("GROK_AGENT_BINARY", None)
    argv = [sys.executable, str(_WRAPPER)] + mode_args
    start = time.monotonic()
    completed = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
        check=False,
    )
    duration = time.monotonic() - start
    stdout = completed.stdout or ""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(
            "wrapper stdout was not a single JSON envelope: {}; stderr tail: {}".format(
                exc, "\n".join((completed.stderr or "").splitlines()[-5:])
            )
        )
    if not isinstance(envelope, dict):
        raise ProbeError("wrapper stdout JSON was not an object")
    return completed.returncode, envelope, completed.stderr or "", duration


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def _envelope_highlights(envelope: Dict[str, object], duration: float) -> Dict[str, object]:
    """Pull the redaction-safe, load-bearing fields out of an envelope for the evidence record."""
    grok = envelope.get("grok") if isinstance(envelope.get("grok"), dict) else {}
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    sandbox = envelope.get("sandbox") if isinstance(envelope.get("sandbox"), dict) else {}
    policy = envelope.get("policy") if isinstance(envelope.get("policy"), dict) else {}
    cleanup = envelope.get("cleanup") if isinstance(envelope.get("cleanup"), dict) else {}
    model_usage = grok.get("modelUsage") if isinstance(grok.get("modelUsage"), dict) else {}
    return {
        "status": envelope.get("status"),
        "runId": envelope.get("runId"),
        "effectiveModel": envelope.get("effectiveModel"),
        "stopReason": grok.get("stopReason"),
        "sessionId": grok.get("sessionId"),
        "requestId": grok.get("requestId"),
        "modelUsageKeys": sorted(model_usage.keys()),
        "usageTurns": usage.get("turns"),
        "usageRaw": usage.get("raw"),
        "sandboxProfile": sandbox.get("reportedProfile"),
        "sandboxEnforced": sandbox.get("enforced"),
        "sandboxEvidence": sandbox.get("evidence"),
        "policyWebAccess": policy.get("webAccess"),
        "cleanupStatus": cleanup.get("status"),
        "latencySeconds": round(duration, 3),
    }


def _read_progress_phases(envelope: Dict[str, object]) -> List[str]:
    """Read the run's progress.jsonl and return the ordered list of phase tokens."""
    path_value = envelope.get("progressStreamPath")
    if not isinstance(path_value, str) or not path_value:
        return []
    path = pathlib.Path(path_value)
    if not path.is_file():
        return []
    phases: List[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("phase"), str):
            phases.append(event["phase"])
    return phases
