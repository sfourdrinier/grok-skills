# wrapper/scripts/groklib/grokcli_version.py
#
# C6 version-pin enforcement: read the accepted-version pin and verify the
# installed Grok binary matches it exactly, failing closed as version-mismatch.
# Split out of grokcli.py (one clear responsibility per file, 900-line cap): this
# has no process-orchestration or stream concerns, only the pin file and a single
# `grok --version` probe. grokcli re-exports accepted_version / check_version so
# callers keep using grokcli.check_version(...).

import json
import pathlib
import subprocess

from groklib import GrokWrapperError, log_stderr

# C6 pin file: wrapper/accepted-version.json. __file__ is
# .../grok-cli/scripts/groklib/grokcli_version.py, so parents[2] is the grok-cli
# skill root (same anchor as grokcli.py, which also lives in groklib/).
ACCEPTED_VERSION_FILE = pathlib.Path(__file__).resolve().parents[2] / "accepted-version.json"

_VERSION_TIMEOUT_SECONDS = 30

_REVALIDATION_HINT = (
    "run the live compatibility revalidation suite to re-pin accepted-version.json before running Grok"
)


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "grokcli" component prefix."""
    log_stderr("grokcli", function, message)


def accepted_version() -> str:
    """Read the C6 accepted-version pin, failing closed as version-mismatch.

    A missing, unreadable, malformed, or version-less pin file is a
    fail-closed ``version-mismatch`` whose message names the revalidation
    suite: the wrapper never runs Grok against an unverifiable pin.
    """
    try:
        raw = ACCEPTED_VERSION_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        _log("accepted_version", "cannot read pin file {}: {}".format(ACCEPTED_VERSION_FILE, exc))
        raise GrokWrapperError(
            "version-mismatch",
            "accepted-version pin file is missing or unreadable; {}".format(_REVALIDATION_HINT),
            {"pinFile": str(ACCEPTED_VERSION_FILE)},
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log("accepted_version", "pin file {} is malformed JSON: {}".format(ACCEPTED_VERSION_FILE, exc))
        raise GrokWrapperError(
            "version-mismatch",
            "accepted-version pin file is malformed JSON; {}".format(_REVALIDATION_HINT),
            {"pinFile": str(ACCEPTED_VERSION_FILE)},
        )
    if not isinstance(document, dict):
        _log("accepted_version", "pin file {} is not a JSON object".format(ACCEPTED_VERSION_FILE))
        raise GrokWrapperError(
            "version-mismatch",
            "accepted-version pin file is not a JSON object; {}".format(_REVALIDATION_HINT),
            {"pinFile": str(ACCEPTED_VERSION_FILE)},
        )
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        _log("accepted_version", "pin file {} has no usable version field".format(ACCEPTED_VERSION_FILE))
        raise GrokWrapperError(
            "version-mismatch",
            "accepted-version pin file has no usable version field; {}".format(_REVALIDATION_HINT),
            {"pinFile": str(ACCEPTED_VERSION_FILE)},
        )
    return version


def check_version(binary: pathlib.Path) -> str:
    """Verify the installed Grok matches the accepted pin exactly, failing closed otherwise.

    Runs ``grok --version`` and compares its first line, verbatim, to
    ``accepted_version()`` (C6). Any failure - the binary cannot run, exits
    nonzero, produces no version line, or a mismatched line - is a
    fail-closed ``version-mismatch`` whose message names the revalidation
    suite. The parent env is inherited here because ``--version`` needs no
    private HOME and produces no auth-sensitive side effect.
    """
    expected = accepted_version()
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
            "could not run grok --version; {}".format(_REVALIDATION_HINT),
            {"reason": "version-command-failed"},
        )

    if completed.returncode != 0:
        _log("check_version", "grok --version exited {}".format(completed.returncode))
        raise GrokWrapperError(
            "version-mismatch",
            "grok --version exited with status {}; {}".format(completed.returncode, _REVALIDATION_HINT),
            {"exitStatus": completed.returncode},
        )

    stdout = completed.stdout or ""
    lines = stdout.splitlines()
    first_line = lines[0].strip() if lines else ""
    if first_line != expected:
        _log("check_version", "installed version {!r} != accepted pin {!r}".format(first_line, expected))
        raise GrokWrapperError(
            "version-mismatch",
            "installed grok version {!r} does not match accepted pin {!r}; {}".format(
                first_line, expected, _REVALIDATION_HINT
            ),
            {"installed": first_line, "expected": expected},
        )
    return first_line
