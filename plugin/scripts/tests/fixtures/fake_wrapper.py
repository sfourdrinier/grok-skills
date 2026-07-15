#!/usr/bin/env python3
# plugin/scripts/tests/fixtures/fake_wrapper.py
#
# Test-only stand-in for the hardened Grok wrapper (grok_agent.py), used by the
# grok-companion relay integration tests to exercise the degrade-to-Tier-1 state
# machine WITHOUT invoking real Grok. It mints a run under $XDG_STATE_HOME (so
# every test is fully isolated from the real state root), optionally writes a
# progress.jsonl, and prints exactly one JSON envelope line to stdout.
#
# Behavior is driven by env vars so one fixture covers every timing:
#   GROK_FAKE_BEHAVIOR: "normal" (run dir + progress), "norun" (no run dir at
#     all -> relay finds nothing), "brokenprogress" (run dir, but progress.jsonl
#     is a DIRECTORY so an external read fails).
#   GROK_FAKE_EXIT: process exit code (non-zero -> a failure envelope).
#   GROK_FAKE_SLEEP: seconds to sleep before printing (lets the live poll fire).

import json
import os
import pathlib
import secrets
import signal
import sys
import time


def _runs_dir() -> pathlib.Path:
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "state"
    return base / "grok-skills" / "runs"


def main() -> int:
    argv = sys.argv[1:]
    mode = argv[0] if argv else "reason"

    run_id = None
    if "--run-id" in argv:
        index = argv.index("--run-id")
        if index + 1 < len(argv):
            run_id = argv[index + 1]
    if not run_id:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)

    behavior = os.environ.get("GROK_FAKE_BEHAVIOR", "normal")
    exit_code = int(os.environ.get("GROK_FAKE_EXIT", "0"))

    # F-GATE-ORPHAN test hooks: record this process's PID (so the test can check
    # liveness) and optionally ignore SIGTERM, so ONLY the stop-gate's process-
    # GROUP SIGKILL can end this grandchild -- proving the group-kill reaches the
    # wrapper the companion spawned, not just the immediate companion child.
    pid_file = os.environ.get("GROK_FAKE_PID_FILE")
    if pid_file:
        try:
            with open(pid_file, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
        except OSError as exc:
            sys.stderr.write("fake_wrapper: could not write pid file {}: {}\n".format(pid_file, exc))
    if os.environ.get("GROK_FAKE_IGNORE_SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except (ValueError, OSError, AttributeError) as exc:
            sys.stderr.write("fake_wrapper: could not ignore SIGTERM: {}\n".format(exc))

    progress_path = None
    if behavior != "norun":
        run_dir = _runs_dir() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # Mirror the real wrapper's F-RELAY-RUNID stderr marker so the companion's
        # forward-and-parse handoff path is exercised. Emitted BEFORE any progress
        # so the relay binds to this exact run from the first tick.
        sys.stderr.write("[grok-run-id] {}\n".format(run_id))
        sys.stderr.flush()
        # Optional decoy: a second, lexically-NEWER run dir with distinctive
        # progress. A naive dir-diff would pick the decoy; the marker keeps the
        # relay on the real run.
        decoy_run_id = os.environ.get("GROK_FAKE_DECOY_RUN_ID")
        if decoy_run_id:
            decoy_dir = _runs_dir() / decoy_run_id
            decoy_dir.mkdir(parents=True, exist_ok=True)
            with open(decoy_dir / "progress.jsonl", "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "runId": decoy_run_id,
                            "seq": 1,
                            "ts": "t",
                            "phase": "start",
                            "level": "info",
                            "message": "DECOY run created",
                        }
                    )
                    + "\n"
                )
                handle.flush()
        progress_path = run_dir / "progress.jsonl"
        if behavior == "brokenprogress":
            progress_path.mkdir(exist_ok=True)
        else:
            events = [
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "seq": 1,
                    "ts": "t",
                    "phase": "start",
                    "level": "info",
                    "message": "{} run created".format(mode),
                    "data": {"mode": mode},
                },
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "seq": 2,
                    "ts": "t",
                    "phase": "grok",
                    "level": "info",
                    "message": "grok streamed thought tokens",
                    "data": {"event": "thought", "chars": 19, "text": "thinking about PONG"},
                },
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "seq": 3,
                    "ts": "t",
                    "phase": "done",
                    "level": "info",
                    "message": "run complete",
                },
            ]
            with open(progress_path, "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
                    handle.flush()

    # Optional large stderr payload: proves the companion + stop gate do not
    # ENOBUFS-kill the child on a long streaming review (F-GATE-MAXBUF). Written
    # after the envelope's run is otherwise set up, before the final stdout line.
    stderr_bytes = int(os.environ.get("GROK_FAKE_STDERR_BYTES", "0"))
    if stderr_bytes > 0:
        sys.stderr.write("x" * stderr_bytes)
        sys.stderr.write("\n")
        sys.stderr.flush()

    time.sleep(float(os.environ.get("GROK_FAKE_SLEEP", "0")))

    status = "failure" if exit_code != 0 else "success"
    envelope = {
        "schemaVersion": 1,
        "runId": run_id,
        "mode": mode,
        "status": status,
        "progressStreamPath": (str(progress_path) if progress_path else None),
    }
    if status == "failure":
        envelope["error"] = {"class": "cli-failure", "message": "fake failure", "detail": None}
    else:
        # Stop-gate requires machine-readable findings (or verify pass). Empty
        # findings = clean review for integration tests.
        envelope["response"] = {
            "structured": {"findings": [], "summary": "fake clean review"},
            "text": "fake clean review",
        }

    print(json.dumps(envelope))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
