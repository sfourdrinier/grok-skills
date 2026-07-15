#!/usr/bin/env python3
# wrapper/scripts/tests/fake_grok.py
#
# C9 fake Grok CLI: a stdlib-only, executable stand-in for the real grok
# binary that groklib.grokcli spawns. It NEVER touches the network, the real
# ~/.grok, or any model; it just replays deterministic, Task-0-shaped output
# so grokcli.execute / inspect_home / probe_login / check_version can be
# exercised in isolation.
#
# The single hard rule (C9): the full argv plus the selected env keys (HOME,
# and the mere PRESENCE of GROK_SANDBOX) are appended as one JSON line to the
# argv log BEFORE any scenario behaviour runs, so a test can prove exactly
# what argv and child env grokcli built.
#
# Steering channel. grokcli.build_child_env constructs a MINIMAL child env
# (only HOME/PATH/TMPDIR; never an os.environ passthrough - that minimality
# is itself under test), so the usual FAKE_GROK_* env vars do NOT reach this
# process when it is spawned by execute/inspect_home/probe_login. The fake
# therefore reads its controls from a JSON control file at
# ``$HOME/fake-grok-control.json`` (HOME is the per-run private home the test
# owns), with the FAKE_GROK_* env vars kept as an override for direct
# invocations (e.g. check_version, which inherits the parent env because it
# has no private home to key a minimal env off). Keys: ``scenario``,
# ``argvLog``, ``editTarget``, ``structured`` (a JSON string), ``version``.

import json
import os
import pathlib
import signal
import sys
import time

_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
_ACCEPTED_VERSION_FILE = pathlib.Path(__file__).resolve().parents[2] / "accepted-version.json"
_CONTROL_FILENAME = "fake-grok-control.json"

# The logged-in `grok models` transcript captured by Task 0 (probe-report.md
# Step 2), and a plausible logged-out variant for the models-loggedout
# scenario. probe_login keys auth state off the "You are logged in" line.
_MODELS_OK = (
    "You are logged in with grok.com.\n"
    "\n"
    "Default model: grok-4.5\n"
    "\n"
    "Available models:\n"
    "  * grok-4.5 (default)\n"
    "  - grok-composer-2.5-fast\n"
)
_MODELS_LOGGEDOUT = (
    "You are not logged in.\n"
    "Run `grok login` to authenticate with grok.com.\n"
)


def _load_control() -> "dict":
    """Read the JSON control file from $HOME, returning {} when absent or unreadable."""
    home = os.environ.get("HOME")
    if not home:
        return {}
    control_path = pathlib.Path(home) / _CONTROL_FILENAME
    if not control_path.is_file():
        return {}
    try:
        loaded = json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write("fake_grok: could not read control file {}: {}\n".format(control_path, exc))
        return {}
    if not isinstance(loaded, dict):
        sys.stderr.write("fake_grok: control file {} is not a JSON object\n".format(control_path))
        return {}
    return loaded


def _control_value(control: "dict", key: str, env_key: str) -> "str|None":
    """Resolve one control value: the FAKE_GROK_* env var wins, else the control file."""
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    value = control.get(key)
    if value is None:
        return None
    return str(value)


def _write_argv_log(argv_log_path: "str|None") -> None:
    """Append the full argv plus selected env keys as one JSON line (C9), always first."""
    if not argv_log_path:
        return
    record = {
        "argv": list(sys.argv),
        "HOME": os.environ.get("HOME"),
        "GROK_SANDBOX_present": "GROK_SANDBOX" in os.environ,
    }
    try:
        with open(argv_log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
    except OSError as exc:
        sys.stderr.write("fake_grok: could not append argv log {}: {}\n".format(argv_log_path, exc))


def _accepted_pin_version() -> str:
    """Return the accepted-version pin's first-line version string, or a sentinel on failure."""
    try:
        document = json.loads(_ACCEPTED_VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write("fake_grok: could not read accepted-version pin: {}\n".format(exc))
        return "grok 0.0.0 (unknown) [fake]"
    version = document.get("version")
    if not isinstance(version, str):
        return "grok 0.0.0 (unknown) [fake]"
    return version


def _load_base_payload() -> "dict":
    """Load the Task 0 `--output-format json` success payload from the captured fixture."""
    return json.loads((_FIXTURES_DIR / "real-output-shape.json").read_text(encoding="utf-8"))


def _load_inspect_shape() -> "dict":
    """Load the Task 0 `grok inspect --json` config-surface document from the captured fixture."""
    return json.loads((_FIXTURES_DIR / "inspect-shape.json").read_text(encoding="utf-8"))


def _arg_value(flag: str) -> "str|None":
    """Return the value token following ``flag`` in argv, or None when absent."""
    argv = sys.argv
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _emit_json(payload: "dict") -> None:
    """Write one JSON document plus a trailing newline to stdout."""
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _streaming_requested() -> bool:
    """True when the wrapper asked for --output-format streaming-json (D-STREAM / T2-0)."""
    return _arg_value("--output-format") == "streaming-json"


def _emit_line(obj: "dict") -> None:
    """Write one streaming-json event line plus a newline, flushed (simulating live arrival)."""
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _split_chunks(text: str, count: int = 3) -> "list":
    """Split ``text`` into up to ``count`` contiguous, order-preserving chunks (empty -> [])."""
    if not text:
        return []
    size = max(1, (len(text) + count - 1) // count)
    return [text[index : index + size] for index in range(0, len(text), size)]


def _emit_stream(payload: "dict") -> None:
    """Replay a `--output-format json` payload as the real streaming-json event set (T2-0.0 shape).

    Emits the payload's ``thought`` and ``text`` as token chunks, then a terminal
    ``end`` event carrying every remaining top-level key (stopReason, sessionId,
    requestId, num_turns, usage, modelUsage, and structuredOutput / changedFiles
    when present). The wrapper's StreamAssembler reconstructs a parsed dict equal
    to this payload minus ``thought`` (which the envelope never consumes), so the
    assembled envelope is equivalent to the single-blob json path.
    """
    thought = payload.get("thought")
    text = payload.get("text")
    for chunk in _split_chunks(thought if isinstance(thought, str) else ""):
        _emit_line({"type": "thought", "data": chunk})
    for chunk in _split_chunks(text if isinstance(text, str) else ""):
        _emit_line({"type": "text", "data": chunk})
    end = {"type": "end"}
    for key, value in payload.items():
        if key in ("thought", "text"):
            continue
        end[key] = value
    _emit_line(end)


def _emit_result(payload: "dict") -> None:
    """Emit a run payload as a streaming-json stream or a single json blob per the requested format."""
    if _streaming_requested():
        _emit_stream(payload)
    else:
        _emit_json(payload)


def _resolve_structured(control: "dict", default: "dict") -> "dict":
    """Resolve the structuredOutput object for schema scenarios (control/env override, else default)."""
    override = _control_value(control, "structured", "FAKE_GROK_STRUCTURED")
    if override is None:
        return default
    try:
        parsed = json.loads(override)
    except ValueError as exc:
        sys.stderr.write("fake_grok: ignoring non-JSON structured override: {}\n".format(exc))
        return default
    if not isinstance(parsed, dict):
        sys.stderr.write("fake_grok: ignoring non-object structured override\n")
        return default
    return parsed


def _scenario_ok_json(control: "dict") -> int:
    _emit_result(_load_base_payload())
    return 0


def _scenario_ok_schema(control: "dict") -> int:
    payload = _load_base_payload()
    structured = _resolve_structured(control, {"answer": "PONG"})
    payload["structuredOutput"] = structured
    payload["text"] = json.dumps(structured)
    _emit_result(payload)
    return 0


def _scenario_stderr_warn(control: "dict") -> int:
    sys.stderr.write("warning: grok emitted a non-fatal diagnostic during the run\n")
    sys.stderr.flush()
    _emit_result(_load_base_payload())
    return 0


def _scenario_no_stdout(control: "dict") -> int:
    return 0


def _scenario_malformed_json(control: "dict") -> int:
    # Streaming: a valid thought then a non-JSON line (no terminal) -> the
    # wrapper classifies the run as output-malformed (unparseable stream line).
    if _streaming_requested():
        _emit_line({"type": "thought", "data": "working"})
    sys.stdout.write("this is not valid json at all {\n")
    sys.stdout.flush()
    return 0


def _scenario_partial_json(control: "dict") -> int:
    # Streaming: a valid thought then a TORN (truncated, unparseable, no-newline)
    # line and no terminal event -> output-malformed. Non-streaming: half a blob.
    if _streaming_requested():
        _emit_line({"type": "thought", "data": "partial"})
        torn = json.dumps({"type": "text", "data": "hello world"})
        sys.stdout.write(torn[: max(1, len(torn) // 2)])
        sys.stdout.flush()
        return 0
    full = json.dumps(_load_base_payload())
    sys.stdout.write(full[: max(1, len(full) // 2)])
    sys.stdout.flush()
    return 0


def _scenario_malformed_only(control: "dict") -> int:
    # Streaming: ONLY non-JSON lines, no valid event at all, exit 0. The
    # assembler never sees a parseable object (saw_any_line stays False) while
    # `malformed` is set -> the wrapper must classify output-malformed, NOT
    # output-missing (there WAS output; it was unparseable). Regression guard for
    # F-STREAM-MALFORMED's malformed-before-empty ordering.
    sys.stdout.write("this is not valid json at all {\n")
    sys.stdout.write("neither is this }}}\n")
    sys.stdout.flush()
    return 0


def _scenario_invalid_utf8(control: "dict") -> int:
    # F-STREAM-DECODE: emit a valid thought, then a raw non-UTF-8 byte on stdout
    # so the wrapper's strict-UTF-8 readline() raises UnicodeDecodeError. The
    # wrapper must classify this output-malformed (fail closed), never crash out
    # of its own outcome classification.
    if _streaming_requested():
        _emit_line({"type": "thought", "data": "working"})
    sys.stdout.flush()
    try:
        sys.stdout.buffer.write(b"\xff\xfe not valid utf-8\n")
        sys.stdout.buffer.flush()
    except (OSError, ValueError) as exc:
        sys.stderr.write("fake_grok: could not write invalid utf-8 bytes: {}\n".format(exc))
    return 0


def _scenario_stream_no_terminal(control: "dict") -> int:
    # A streaming run of ALL-valid thought/text lines that ends WITHOUT a terminal
    # `end` event: a torn stream the wrapper must fail closed as output-malformed
    # (no-terminal-event), never trust as a clean result.
    _emit_line({"type": "thought", "data": "reasoning"})
    _emit_line({"type": "text", "data": "answer so far"})
    return 0


def _scenario_cancelled_stop(control: "dict") -> int:
    payload = _load_base_payload()
    payload["stopReason"] = "Cancelled"
    _emit_result(payload)
    # Exit nonzero on purpose: a Cancelled stop reason must classify as
    # "cancelled" even when the process also exits nonzero, proving the
    # stop-reason classification wins over the raw exit code.
    return 1


def _scenario_model_mismatch(control: "dict") -> int:
    payload = _load_base_payload()
    payload["modelUsage"] = {
        "grok-unexpected-model": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadInputTokens": 0,
            "modelCalls": 1,
        }
    }
    _emit_result(payload)
    return 0


def _scenario_verifier_missing(control: "dict") -> int:
    # A verify-mode reply that omits the structured verdict entirely.
    _emit_result(_load_base_payload())
    return 0


def _scenario_sandbox_fail_open(control: "dict") -> int:
    home = os.environ.get("HOME")
    if home:
        grok_dir = pathlib.Path(home) / ".grok"
        event = {
            "timestamp": "2026-07-14T00:00:00Z",
            "event_type": "ProfileApplied",
            "profile": "workspace",
            "workspace": home,
            "platform": "macos/seatbelt",
            "enforced": False,
            "restrict_network": False,
            "read_write_paths": [],
            "read_only_paths": [],
        }
        try:
            grok_dir.mkdir(parents=True, exist_ok=True)
            with open(grok_dir / "sandbox-events.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
                handle.flush()
        except OSError as exc:
            sys.stderr.write("fake_grok: could not write sandbox-events.jsonl: {}\n".format(exc))
    _emit_result(_load_base_payload())
    return 0


def _scenario_sleep_forever(control: "dict") -> int:
    # Ignore SIGTERM so that ONLY the process-group SIGKILL delivered by
    # platformsupport.kill_process_tree can end this process; this proves the
    # timeout path actually kills the tree rather than relying on a catchable
    # terminate. The argv log line was already written by main() first. No
    # stdout is emitted before the sleep, so the wrapper's read loop blocks and
    # only the watchdog timeout can end the wait.
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError, AttributeError) as exc:
        sys.stderr.write("fake_grok: could not ignore SIGTERM: {}\n".format(exc))
    time.sleep(5)
    _emit_result(_load_base_payload())
    return 0


def _scenario_nonzero_exit(control: "dict") -> int:
    sys.stderr.write("fatal: grok cli failed to complete the request\n")
    sys.stderr.flush()
    return 3


def _scenario_turn_exhaustion(control: "dict") -> int:
    payload = _load_base_payload()
    payload["stopReason"] = "MaxTurns"
    max_turns = _arg_value("--max-turns")
    if max_turns is not None:
        try:
            payload["num_turns"] = int(max_turns)
        except ValueError:
            sys.stderr.write("fake_grok: non-integer --max-turns {!r}\n".format(max_turns))
    _emit_result(payload)
    return 0


def _scenario_writes_outside(control: "dict") -> int:
    target = _control_value(control, "editTarget", "FAKE_GROK_EDIT_TARGET")
    if target:
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("edited by fake grok\n")
                handle.flush()
        except OSError as exc:
            sys.stderr.write("fake_grok: could not write edit target {}: {}\n".format(target, exc))
    _emit_result(_load_base_payload())
    return 0


def _scenario_secret_in_output(control: "dict") -> int:
    # A review reply that legitimately QUOTES a secret-shaped string it read from
    # a repo file. The wrapper must redact-and-REPORT it (dogfood-2 #3), not
    # hard-fail the whole run and lose the body.
    payload = _load_base_payload()
    payload["text"] = "The config still contains a live key: sk-ABCDEF0123456789ABCDEFGHIJKLMNOP -- rotate it."
    _emit_result(payload)
    return 0


def _scenario_writes_into_cwd(control: "dict") -> int:
    # A read-only run that ACTUALLY writes a file into its cwd (the repo target)
    # while reporting NO edits in its JSON output. The FS drift audit should WARN
    # (not fail) so a completed review is not discarded.
    try:
        with open("grok_sneaky_write.txt", "w", encoding="utf-8") as handle:
            handle.write("sneaky\n")
            handle.flush()
    except OSError as exc:
        sys.stderr.write("fake_grok: could not write sneaky file: {}\n".format(exc))
    _emit_result(_load_base_payload())
    return 0


def _scenario_writes_into_cwd_then_malformed(control: "dict") -> int:
    # A read-only run that writes into its cwd AND THEN emits malformed output.
    # Failure class must remain output-malformed; FS drift is only a warning.
    try:
        with open("grok_sneaky_write.txt", "w", encoding="utf-8") as handle:
            handle.write("sneaky\n")
            handle.flush()
    except OSError as exc:
        sys.stderr.write("fake_grok: could not write sneaky file: {}\n".format(exc))
    if _streaming_requested():
        _emit_line({"type": "thought", "data": "working"})
    sys.stdout.write("this is not valid json at all {\n")
    sys.stdout.flush()
    return 0


def _scenario_reports_file_change(control: "dict") -> int:
    # A read-only review reply that lists a change-shaped JSON key; review must
    # stay success with an informational note (not unexpected-edits).
    payload = _load_base_payload()
    payload["changedFiles"] = ["pkg/module.txt"]
    _emit_result(payload)
    return 0


def _scenario_inspect_ok(control: "dict") -> int:
    _emit_json(_load_inspect_shape())
    return 0


def _scenario_models_ok(control: "dict") -> int:
    sys.stdout.write(_MODELS_OK)
    sys.stdout.flush()
    return 0


def _scenario_models_loggedout(control: "dict") -> int:
    sys.stdout.write(_MODELS_LOGGEDOUT)
    sys.stdout.flush()
    return 0


_SCENARIOS = {
    "ok-json": _scenario_ok_json,
    "ok-schema": _scenario_ok_schema,
    "stderr-warn": _scenario_stderr_warn,
    "no-stdout": _scenario_no_stdout,
    "malformed-json": _scenario_malformed_json,
    "malformed-only": _scenario_malformed_only,
    "invalid-utf8": _scenario_invalid_utf8,
    "partial-json": _scenario_partial_json,
    "stream-no-terminal": _scenario_stream_no_terminal,
    "cancelled-stop": _scenario_cancelled_stop,
    "model-mismatch": _scenario_model_mismatch,
    "verifier-missing": _scenario_verifier_missing,
    "sandbox-fail-open": _scenario_sandbox_fail_open,
    "sleep-forever": _scenario_sleep_forever,
    "nonzero-exit": _scenario_nonzero_exit,
    "turn-exhaustion": _scenario_turn_exhaustion,
    "writes-outside": _scenario_writes_outside,
    "reports-file-change": _scenario_reports_file_change,
    "secret-in-output": _scenario_secret_in_output,
    "writes-into-cwd": _scenario_writes_into_cwd,
    "writes-into-cwd-then-malformed": _scenario_writes_into_cwd_then_malformed,
    "inspect-ok": _scenario_inspect_ok,
    "models-ok": _scenario_models_ok,
    "models-loggedout": _scenario_models_loggedout,
}


def main() -> int:
    control = _load_control()

    # Argv log is written FIRST (C9), before any --version or scenario branch.
    _write_argv_log(_control_value(control, "argvLog", "FAKE_GROK_ARGV_LOG"))

    if "--version" in sys.argv:
        version = _control_value(control, "version", "FAKE_GROK_VERSION") or _accepted_pin_version()
        sys.stdout.write(version + "\n")
        sys.stdout.flush()
        return 0

    scenario = _control_value(control, "scenario", "FAKE_GROK_SCENARIO") or ""
    handler = _SCENARIOS.get(scenario)
    if handler is None:
        sys.stderr.write("fake_grok: unknown or missing scenario {!r}\n".format(scenario))
        return 2
    return handler(control)


if __name__ == "__main__":
    sys.exit(main())
