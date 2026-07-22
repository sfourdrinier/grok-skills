# wrapper/scripts/groklib/grokcli_version.py
#
# Verify the installed Grok CLI is runnable. Does NOT hard-pin a specific CLI
# build: Grok ships frequently and a public plugin must accept any working
# install. `accepted-version.json` is last-validated maintainer evidence only
# (advisory), never a runtime allowlist.
#
# Split out of grokcli.py (one clear responsibility per file). grokcli re-exports
# accepted_version / check_version so callers keep using grokcli.check_version(...).

import json
import pathlib
import re
import subprocess
from typing import Optional

from groklib import GrokWrapperError, log_stderr

# Last-validated evidence file (NOT a runtime gate). Path is relative to the
# wrapper skill root (…/wrapper/accepted-version.json).
ACCEPTED_VERSION_FILE = pathlib.Path(__file__).resolve().parents[2] / "accepted-version.json"

_VERSION_TIMEOUT_SECONDS = 30

# Fail closed only when --version itself is unusable - not when it differs from
# a maintainer probe stamp.
_VERSION_HINT = (
    "install the Grok CLI and confirm `grok --version` prints a version line"
)

# Accept lines that look like real Grok CLI output (not empty / garbage).
_GROK_VERSION_RE = re.compile(r"^grok\b", re.IGNORECASE)


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "grokcli" component prefix."""
    log_stderr("grokcli", function, message)


def last_validated_version() -> Optional[str]:
    """Read the optional last-validated version stamp (advisory only).

    Missing or malformed files return ``None`` - they never block a run.
    """
    try:
        raw = ACCEPTED_VERSION_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    return version.strip()


def accepted_version() -> str:
    """Backward-compatible name for the last-validated stamp when present.

    Prefer :func:`last_validated_version` for new code. Returns empty string when
    no stamp is available (never raises for a missing pin).
    """
    return last_validated_version() or ""


def check_version(binary: pathlib.Path) -> str:
    """Verify the installed Grok CLI runs and reports a version line.

    Runs ``grok --version`` and returns its first line. Failures only when:

    - the binary cannot be executed
    - ``--version`` exits nonzero
    - stdout has no usable version line (empty or not Grok-shaped)

    A version that differs from ``accepted-version.json`` is **allowed**. That
    file is last-validated maintainer evidence, not an allowlist. When a stamp
    exists and differs, a stderr log note is emitted for diagnostics only.
    """
    argv = [str(binary), "--version"]
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_VERSION_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log("check_version", "could not run grok --version: {}".format(exc))
        raise GrokWrapperError(
            "version-mismatch",
            "could not run grok --version; {}".format(_VERSION_HINT),
            {"reason": "version-command-failed"},
        )

    if completed.returncode != 0:
        _log("check_version", "grok --version exited {}".format(completed.returncode))
        raise GrokWrapperError(
            "version-mismatch",
            "grok --version exited with status {}; {}".format(completed.returncode, _VERSION_HINT),
            {"exitStatus": completed.returncode},
        )

    stdout = completed.stdout or ""
    lines = stdout.splitlines()
    first_line = lines[0].strip() if lines else ""
    if not first_line or not _GROK_VERSION_RE.match(first_line):
        _log("check_version", "unusable grok --version output {!r}".format(first_line))
        raise GrokWrapperError(
            "version-mismatch",
            "grok --version did not report a usable version line; {}".format(_VERSION_HINT),
            {"installed": first_line, "reason": "version-output-unusable"},
        )

    reference = last_validated_version()
    if reference and first_line != reference:
        _log(
            "check_version",
            "installed version {!r} differs from last-validated stamp {!r} "
            "(allowed; stamp is advisory only)".format(first_line, reference),
        )
    return first_line


def maybe_refresh_accepted_version_stamp(version: str) -> bool:
    """Best-effort write of advisory accepted-version.json after green preflight.

    Issue #8: after a fully green preflight on a new CLI version, refresh the
    advisory stamp so the diagnostic disappears until the next bump. Never fails
    the run if the install tree is read-only. Not called from check_version so
    ordinary runs and unit tests never mutate the package stamp.
    """
    if not isinstance(version, str) or not version.strip():
        return False
    version = version.strip()
    current = last_validated_version()
    if current == version:
        return True
    # Preserve existing maintainer fields (schemaVersion, probeEvidence, …).
    payload: dict = {}
    try:
        raw = ACCEPTED_VERSION_FILE.read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            payload = dict(loaded)
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload["version"] = version
    payload.setdefault("schemaVersion", 2)
    payload.setdefault("enforcement", "none")
    payload.setdefault(
        "note",
        "Last maintainer-validated Grok CLI build (advisory only). Runtime does "
        "NOT require this exact version - any working `grok --version` is accepted.",
    )
    payload["selfHealedAtUtc"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        ACCEPTED_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCEPTED_VERSION_FILE.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _log(
            "maybe_refresh_accepted_version_stamp",
            "refreshed advisory stamp to {!r}".format(version),
        )
        return True
    except OSError as exc:
        _log(
            "maybe_refresh_accepted_version_stamp",
            "could not refresh stamp (ignored): {}".format(exc),
        )
        return False
