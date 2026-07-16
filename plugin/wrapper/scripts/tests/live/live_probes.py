#!/usr/bin/env python3
# wrapper/scripts/tests/live/live_probes.py
#
# Task 13/14 live probe suite. This is a MANUALLY invoked script, NOT a
# unittest module: it lives under tests/live/ with a non-test_* name so
# `python3 -m unittest discover -s tests -t .` never picks it up. It drives the
# FINISHED wrapper (scripts/grok_agent.py) end to end against the REAL Grok CLI
# (real ~/.grok auth material, real ~/.grok/bin/grok binary) and asserts on the
# returned C4 envelopes. The read-only probes never edit the host checkout (the one
# host-checkout-touching probe, `review`, is read-only by construction and is
# asserted to leave `git status` byte-identical). The write-capable code/verify
# handoff probe (Task 14) NEVER runs against the host checkout: it scaffolds a
# DISPOSABLE temp git repo, runs the real write-capable `code` there under an
# isolated XDG_STATE_HOME, then runs `verify` over the SAME worktree (the real
# code->verify handoff) and force-removes the retained worktree afterward. It
# never reads/prints/stores authentication file contents, and cleans up every
# temp home it creates (authentication material removed FIRST). Redaction: the C4 envelope carries no account identifiers by
# construction (envelope.assert_no_secret_material gates every wrapper output),
# and this script records only run ids, ephemeral per-run session/request UUIDs,
# stop reasons, token counts, and latencies -- never auth bytes.
#
# Usage:
#   python3 live_probes.py                 run every probe, print a summary,
#                                           exit 0 iff every GATING probe passed;
#                                           the last-validated stamp is NOT touched.
#   python3 live_probes.py --revalidate    run every probe and, ONLY on a fully
#                                           green gating run, rewrite
#                                           accepted-version.json (advisory stamp)
#                                           with the installed version, timestamp,
#                                           evidence pointer, enforcement: none.
#                                           A red run leaves the stamp untouched.
#   python3 live_probes.py --evidence-out PATH   also write the machine-readable
#                                           evidence JSON to PATH.
#
# Python stdlib only; 3.9 syntax. No em/en dashes. No empty excepts.

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Dict, List, Optional, Tuple

import code_verify_probe
from _probe_common import (
    ProbeError,
    ProbeResult,
    _ACCEPTED_VERSION_FILE,
    _GROK_BINARY,
    _SKILL_ROOT,
    _SOURCE_AUTH,
    _VERDICT_SCHEMA,
    _VERSION_PATTERN,
    _envelope_highlights,
    _installed_version_first_line,
    _read_progress_phases,
    _require,
    _run_wrapper,
    _utc_now_iso_z,
)

# The evidence pointer written into accepted-version.json on a green --revalidate
# run. Points at the live suite that validated the pin (stable path, no churn).
_EVIDENCE_POINTER = "scripts/tests/live/README.md"

# Per-probe wall-clock ceiling for a live model call. Generous so a slow (but
# progressing) model turn is never mistaken for a hang. A real hang is killed by
# the wrapper's own inner timeout long before this outer guard fires.
_PROBE_TIMEOUT_SECONDS = 300
_RAW_CHECK_TIMEOUT_SECONDS = 240


# ---------------------------------------------------------------------------
# Gating probes
# ---------------------------------------------------------------------------


def probe_preflight() -> ProbeResult:
    """Probe 1: preflight succeeds; version pin satisfied; secretReadDenial advisory present; macOS probed."""
    command = "grok_agent.py preflight"
    exit_code, envelope, _stderr, duration = _run_wrapper(["preflight"], _PROBE_TIMEOUT_SECONDS)
    highlights = _envelope_highlights(envelope, duration)
    try:
        _require(exit_code == 0, "expected exit 0, got {}".format(exit_code))
        _require(envelope.get("status") == "success", "expected status success")
        response = envelope.get("response")
        _require(isinstance(response, dict), "response is not an object")
        checks = response.get("checks") if isinstance(response, dict) else None
        _require(isinstance(checks, list), "response.checks is not a list")
        by_name = {c.get("name"): c for c in checks if isinstance(c, dict)}
        _require("grokVersion" in by_name and by_name["grokVersion"].get("ok") is True, "grokVersion check not ok")
        version_detail = by_name["grokVersion"].get("detail")
        _require(
            isinstance(version_detail, str) and version_detail == _installed_version_first_line(),
            "grokVersion detail does not match the installed version line",
        )
        secret = by_name.get("secretReadDenial")
        _require(isinstance(secret, dict), "secretReadDenial check absent")
        _require(secret.get("value") is False, "secretReadDenial advisory value is not false")
        platform = response.get("platform")
        _require(platform == "macos", "probed platform is not macos: {}".format(platform))
        _require(response.get("platformProbed") is True, "current platform is not marked probed")
        phases = _read_progress_phases(envelope)
        _require("start" in phases and "done" in phases, "progress stream missing start/done phases")
        highlights["checkNames"] = sorted(by_name.keys())
        highlights["progressPhases"] = phases
        highlights["versionPin"] = version_detail
        return ProbeResult("preflight", True, True, command, highlights, "preflight green; pin satisfied")
    except ProbeError as exc:
        highlights["error"] = envelope.get("error")
        return ProbeResult("preflight", True, False, command, highlights, str(exc))


def probe_reason_isolated() -> ProbeResult:
    """Probe 2: reason PONG in isolation; effectiveModel grok-4.5*; no changedFiles; start..done; home destroyed."""
    command = 'grok_agent.py reason --task "Reply with exactly: PONG"'
    exit_code, envelope, _stderr, duration = _run_wrapper(
        ["reason", "--task", "Reply with exactly: PONG", "--max-turns", "3"], _PROBE_TIMEOUT_SECONDS
    )
    highlights = _envelope_highlights(envelope, duration)
    try:
        _require(exit_code == 0, "expected exit 0, got {}".format(exit_code))
        _require(envelope.get("status") == "success", "expected status success")
        effective = envelope.get("effectiveModel")
        _require(
            isinstance(effective, str) and effective.startswith("grok-4.5"),
            "effectiveModel does not start with grok-4.5: {}".format(effective),
        )
        changed = envelope.get("changedFiles")
        _require(isinstance(changed, list) and not changed, "changedFiles is not empty")
        grok = envelope.get("grok") if isinstance(envelope.get("grok"), dict) else {}
        _require(grok.get("stopReason") != "Cancelled", "run was cancelled")
        phases = _read_progress_phases(envelope)
        _require("start" in phases and "done" in phases, "progress stream missing start/done phases")
        cleanup = envelope.get("cleanup") if isinstance(envelope.get("cleanup"), dict) else {}
        _require(cleanup.get("status") == "clean", "private home not cleanly destroyed: {}".format(cleanup))
        response = envelope.get("response") if isinstance(envelope.get("response"), dict) else {}
        highlights["responseText"] = response.get("text")
        highlights["progressPhases"] = phases
        return ProbeResult("reason-isolated", True, True, command, highlights, "reason PONG green; home clean")
    except ProbeError as exc:
        highlights["error"] = envelope.get("error")
        return ProbeResult("reason-isolated", True, False, command, highlights, str(exc))


def probe_reason_structured(tmp_dir: pathlib.Path) -> ProbeResult:
    """Probe 3: reason --schema (verify verdict schema) extracts structured output live at response.structured."""
    schema_path = tmp_dir / "verdict-schema.json"
    schema_path.write_text(json.dumps(_VERDICT_SCHEMA), encoding="utf-8")
    task = (
        "Return a structured result matching the provided JSON schema. Set verdict to the "
        'string "pass" and evidence to a one-element array containing the string '
        '"live structured probe succeeded".'
    )
    command = 'grok_agent.py reason --schema verdict-schema.json --task "<verdict request>"'
    exit_code, envelope, _stderr, duration = _run_wrapper(
        ["reason", "--task", task, "--schema", str(schema_path), "--max-turns", "3"], _PROBE_TIMEOUT_SECONDS
    )
    highlights = _envelope_highlights(envelope, duration)
    try:
        _require(exit_code == 0, "expected exit 0, got {}".format(exit_code))
        _require(envelope.get("status") == "success", "expected status success")
        response = envelope.get("response")
        _require(isinstance(response, dict), "response is not an object")
        structured = response.get("structured") if isinstance(response, dict) else None
        _require(isinstance(structured, dict), "response.structured is not an object (structuredOutput location)")
        verdict = structured.get("verdict")
        _require(verdict in ("pass", "fail", "inconclusive"), "structured verdict outside enum: {}".format(verdict))
        evidence = structured.get("evidence")
        _require(
            isinstance(evidence, list) and all(isinstance(item, str) for item in evidence),
            "structured evidence is not an array of strings",
        )
        highlights["structured"] = structured
        highlights["structuredOutputLocation"] = "response.structured (top-level grok structuredOutput)"
        return ProbeResult(
            "reason-structured", True, True, command, highlights, "structured extraction green; verdict={}".format(verdict)
        )
    except ProbeError as exc:
        highlights["error"] = envelope.get("error")
        return ProbeResult("reason-structured", True, False, command, highlights, str(exc))


def probe_reason_web() -> ProbeResult:
    """Probe 3b: reason --web asks for a fast-moving version; policy.webAccess true; answer reflects live data (D-WEB)."""
    task = (
        "Using your web tools, find the current latest stable release version number of the Node.js "
        "runtime. Reply with only that version number, for example 20.11.1."
    )
    command = 'grok_agent.py reason --web --task "<current Node.js stable version>"'
    exit_code, envelope, _stderr, duration = _run_wrapper(
        ["reason", "--web", "--task", task, "--max-turns", "8"], _PROBE_TIMEOUT_SECONDS
    )
    highlights = _envelope_highlights(envelope, duration)
    try:
        _require(exit_code == 0, "expected exit 0, got {}".format(exit_code))
        _require(envelope.get("status") == "success", "expected status success")
        policy = envelope.get("policy") if isinstance(envelope.get("policy"), dict) else {}
        _require(policy.get("webAccess") is True, "policy.webAccess is not true")
        tools = policy.get("tools")
        _require(isinstance(tools, list) and "web_search" in tools, "web_search not in the policy tool allowlist")
        response = envelope.get("response") if isinstance(envelope.get("response"), dict) else {}
        text = response.get("text")
        _require(isinstance(text, str) and text.strip() != "", "web answer text is empty")
        _require(
            _VERSION_PATTERN.search(text) is not None,
            "web answer does not contain a version-like token (live data not reflected): {!r}".format(text[:120]),
        )
        highlights["responseText"] = text.strip()[:200]
        return ProbeResult("reason-web", True, True, command, highlights, "web probe green; answer carries a live version")
    except ProbeError as exc:
        highlights["error"] = envelope.get("error")
        return ProbeResult("reason-web", True, False, command, highlights, str(exc))


def probe_review() -> ProbeResult:
    """Probe 4: review a real in-repo target read-only; instructions carry the root pair; zero repo writes."""
    target = "wrapper"
    task = (
        "Review the file groklib/progress.py in this workspace for defects: correctness bugs, race "
        "conditions, resource leaks, or error-handling gaps. List each concrete finding with the "
        "function name, or state clearly that you found no defects."
    )
    command = 'grok_agent.py review --target wrapper --task "<review progress.py>"'
    status_before = _git_status_porcelain()
    exit_code, envelope, _stderr, duration = _run_wrapper(
        ["review", "--target", target, "--task", task, "--max-turns", "12"], _PROBE_TIMEOUT_SECONDS
    )
    status_after = _git_status_porcelain()
    highlights = _envelope_highlights(envelope, duration)
    try:
        _require(exit_code == 0, "expected exit 0, got {}".format(exit_code))
        _require(envelope.get("status") == "success", "expected status success")
        instructions = envelope.get("instructions")
        _require(isinstance(instructions, list) and instructions, "instructions[] is empty")
        paths = [i.get("path") for i in instructions if isinstance(i, dict)]
        _require("AGENTS.md" in paths, "instructions[] missing the repo-root AGENTS.md/CLAUDE.md pair: {}".format(paths))
        changed = envelope.get("changedFiles")
        _require(isinstance(changed, list) and not changed, "review reported changedFiles")
        _require(status_before == status_after, "git status changed across the review run (repo write detected)")
        response = envelope.get("response") if isinstance(envelope.get("response"), dict) else {}
        highlights["instructionPaths"] = paths
        highlights["gitStatusStable"] = True
        highlights["responseTextHead"] = (response.get("text") or "")[:200] if isinstance(response.get("text"), str) else None
        return ProbeResult("review", True, True, command, highlights, "review green; root pair present; zero repo writes")
    except ProbeError as exc:
        highlights["error"] = envelope.get("error")
        highlights["gitStatusStable"] = status_before == status_after
        return ProbeResult("review", True, False, command, highlights, str(exc))


def _git_status_porcelain() -> str:
    completed = subprocess.run(
        ["git", "-C", str(_SKILL_ROOT), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return completed.stdout or ""


def probe_parallel_isolation() -> ProbeResult:
    """Probe 5: two concurrent reason runs succeed with distinct run ids, sessions, homes; neither cancelled."""
    command = "two concurrent grok_agent.py reason --task PONG runs"
    env = dict(os.environ)
    env.pop("GROK_AGENT_BINARY", None)
    argv = [
        sys.executable,
        str(_WRAPPER),
        "reason",
        "--task",
        "Reply with exactly: PONG",
        "--max-turns",
        "3",
    ]
    highlights: Dict[str, object] = {}
    try:
        start = time.monotonic()
        proc_a = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env
        )
        proc_b = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env
        )
        out_a, _err_a = proc_a.communicate(timeout=_PROBE_TIMEOUT_SECONDS)
        out_b, _err_b = proc_b.communicate(timeout=_PROBE_TIMEOUT_SECONDS)
        duration = time.monotonic() - start

        env_a = json.loads(out_a)
        env_b = json.loads(out_b)
        _require(isinstance(env_a, dict) and isinstance(env_b, dict), "a parallel run did not emit a JSON object")
        _require(env_a.get("status") == "success", "parallel run A did not succeed")
        _require(env_b.get("status") == "success", "parallel run B did not succeed")

        run_a = env_a.get("runId")
        run_b = env_b.get("runId")
        _require(isinstance(run_a, str) and isinstance(run_b, str) and run_a != run_b, "run ids not distinct")

        grok_a = env_a.get("grok") if isinstance(env_a.get("grok"), dict) else {}
        grok_b = env_b.get("grok") if isinstance(env_b.get("grok"), dict) else {}
        session_a = grok_a.get("sessionId")
        session_b = grok_b.get("sessionId")
        _require(
            isinstance(session_a, str) and isinstance(session_b, str) and session_a != session_b,
            "session ids not distinct",
        )
        _require(grok_a.get("stopReason") != "Cancelled", "parallel run A was cancelled")
        _require(grok_b.get("stopReason") != "Cancelled", "parallel run B was cancelled")

        cwd_a = env_a.get("effectiveWorkingDirectory")
        cwd_b = env_b.get("effectiveWorkingDirectory")
        _require(
            isinstance(cwd_a, str) and isinstance(cwd_b, str) and cwd_a != cwd_b,
            "isolated working directories not distinct",
        )
        prog_a = env_a.get("progressStreamPath")
        prog_b = env_b.get("progressStreamPath")
        _require(prog_a != prog_b, "progress stream paths not distinct")

        highlights = {
            "runIds": [run_a, run_b],
            "sessionIds": [session_a, session_b],
            "workingDirsDistinct": True,
            "stopReasons": [grok_a.get("stopReason"), grok_b.get("stopReason")],
            "latencySeconds": round(duration, 3),
        }
        return ProbeResult(
            "parallel-isolation", True, True, command, highlights, "both parallel runs green; ids/sessions/homes distinct"
        )
    except (ProbeError, json.JSONDecodeError, subprocess.SubprocessError, OSError) as exc:
        highlights["error"] = str(exc)
        return ProbeResult("parallel-isolation", True, False, command, highlights, str(exc))


# ---------------------------------------------------------------------------
# Informational probes (do NOT gate the green/red result)
# ---------------------------------------------------------------------------


def _make_raw_probe_home() -> Tuple[pathlib.Path, pathlib.Path]:
    """Build an isolated private home (auth.json copied at 0600) plus a temp cwd for a raw-CLI probe."""
    home = pathlib.Path(tempfile.mkdtemp(prefix="grok-skills-checkprobe-home-"))
    os.chmod(str(home), 0o700)
    grok_dir = home / ".grok"
    grok_dir.mkdir(mode=0o700)
    dest_auth = grok_dir / "auth.json"
    # Stream-copy the auth file; never read its bytes into a Python value.
    shutil.copyfile(str(_SOURCE_AUTH), str(dest_auth))
    os.chmod(str(dest_auth), 0o600)
    cwd = pathlib.Path(tempfile.mkdtemp(prefix="grok-skills-checkprobe-cwd-"))
    return home, cwd


def _destroy_raw_probe_home(home: pathlib.Path, cwd: pathlib.Path) -> None:
    """Tear down a raw-probe home: remove the auth copy FIRST, then the directories. Best effort, logged."""
    dest_auth = home / ".grok" / "auth.json"
    try:
        if dest_auth.exists():
            os.remove(str(dest_auth))
    except OSError as exc:
        sys.stderr.write("[live_probes] could not remove raw-probe auth copy: {}\n".format(exc))
    for directory in (home, cwd):
        try:
            shutil.rmtree(str(directory), ignore_errors=True)
        except OSError as exc:
            sys.stderr.write("[live_probes] could not remove raw-probe dir {}: {}\n".format(directory, exc))


def probe_check_deferral() -> ProbeResult:
    """Probe 6 (informational): one RAW-CLI `--check` run in an isolated home; record whether a verifier ran.

    This never goes through the wrapper (C8 keeps --check unexposed in v1). It
    records evidence for the spec section 12 deferral note only; its outcome does
    not gate the suite.
    """
    command = "raw grok --check (isolated home, temp cwd, NOT through the wrapper)"
    home: Optional[pathlib.Path] = None
    cwd: Optional[pathlib.Path] = None
    highlights: Dict[str, object] = {}
    try:
        home, cwd = _make_raw_probe_home()
        prompt_path = cwd / "prompt.txt"
        prompt_path.write_text("Reply with exactly: PONG", encoding="utf-8")
        leader_socket = home / ".grok" / "c.sock"
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", os.defpath),
            "TMPDIR": str(home / "tmp"),
        }
        (home / "tmp").mkdir(mode=0o700, exist_ok=True)
        argv = [
            str(_GROK_BINARY),
            "--prompt-file",
            str(prompt_path),
            "--verbatim",
            "--cwd",
            str(cwd),
            "--output-format",
            "json",
            "--model",
            "grok-4.5",
            "--permission-mode",
            "auto",
            "--no-memory",
            "--disable-web-search",
            "--no-plan",
            "--sandbox",
            "read-only",
            "--check",
            "--max-turns",
            "10",
            "--session-id",
            str(uuid.uuid4()),
            "--leader-socket",
            str(leader_socket),
        ]
        start = time.monotonic()
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=_RAW_CHECK_TIMEOUT_SECONDS,
            env=env,
            cwd=str(cwd),
            check=False,
        )
        duration = time.monotonic() - start
        parsed: Optional[Dict[str, object]] = None
        try:
            candidate = json.loads(completed.stdout or "")
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = None

        num_turns = parsed.get("num_turns") if isinstance(parsed, dict) else None
        stop_reason = parsed.get("stopReason") if isinstance(parsed, dict) else None
        thought = parsed.get("thought") if isinstance(parsed, dict) else None
        # Heuristic: a self-verification loop shows up as more than one turn or a
        # thought that mentions verifying/checking. Recorded as evidence only.
        verifier_ran = bool(isinstance(num_turns, int) and num_turns and num_turns > 1)
        thought_mentions_verify = bool(
            isinstance(thought, str) and re.search(r"(?i)verif|self-check|double-check", thought)
        )
        highlights = {
            "exitStatus": completed.returncode,
            "stopReason": stop_reason,
            "numTurns": num_turns,
            "verifierLikelyRan": verifier_ran or thought_mentions_verify,
            "stderrTail": "\n".join((completed.stderr or "").splitlines()[-3:]),
            "latencySeconds": round(duration, 3),
        }
        detail = "raw --check ran (exit {}, stopReason {}, turns {}); v1 keeps --check unexposed regardless".format(
            completed.returncode, stop_reason, num_turns
        )
        return ProbeResult("check-deferral", False, True, command, highlights, detail)
    except (OSError, subprocess.SubprocessError) as exc:
        highlights["error"] = str(exc)
        return ProbeResult("check-deferral", False, False, command, highlights, "raw --check probe could not run: {}".format(exc))
    finally:
        if home is not None and cwd is not None:
            _destroy_raw_probe_home(home, cwd)


def probe_max_turns_token() -> ProbeResult:
    """Informational: try to trigger a real max-turns hit cheaply via the RAW CLI and record its stop-reason token.

    Uses a read tool with --max-turns 1 so the model cannot both call the tool
    and produce a final answer within the budget. Whatever stop reason comes back
    is recorded so grokcli_output's turn-exhaustion matcher can be pinned against
    a real token; if the run instead ends cleanly or is cancelled, that is
    recorded as "max-turns token remains unverified".
    """
    command = "raw grok --max-turns 1 with a read tool (isolated home) to observe the max-turns stop token"
    home: Optional[pathlib.Path] = None
    cwd: Optional[pathlib.Path] = None
    highlights: Dict[str, object] = {}
    try:
        home, cwd = _make_raw_probe_home()
        note_path = cwd / "note.txt"
        note_path.write_text("alpha-bravo-charlie", encoding="utf-8")
        prompt_path = cwd / "prompt.txt"
        prompt_path.write_text(
            "Read the file note.txt using your read tool, then reply with its exact contents.",
            encoding="utf-8",
        )
        leader_socket = home / ".grok" / "m.sock"
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", os.defpath),
            "TMPDIR": str(home / "tmp"),
        }
        (home / "tmp").mkdir(mode=0o700, exist_ok=True)
        argv = [
            str(_GROK_BINARY),
            "--prompt-file",
            str(prompt_path),
            "--verbatim",
            "--cwd",
            str(cwd),
            "--output-format",
            "json",
            "--model",
            "grok-4.5",
            "--permission-mode",
            "auto",
            "--tools",
            "read_file",
            "--no-subagents",
            "--no-memory",
            "--disable-web-search",
            "--no-plan",
            "--sandbox",
            "read-only",
            "--max-turns",
            "1",
            "--session-id",
            str(uuid.uuid4()),
            "--leader-socket",
            str(leader_socket),
        ]
        start = time.monotonic()
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=_RAW_CHECK_TIMEOUT_SECONDS,
            env=env,
            cwd=str(cwd),
            check=False,
        )
        duration = time.monotonic() - start
        stop_reason: Optional[str] = None
        num_turns: Optional[object] = None
        try:
            candidate = json.loads(completed.stdout or "")
            if isinstance(candidate, dict):
                stop_reason = candidate.get("stopReason") if isinstance(candidate.get("stopReason"), str) else None
                num_turns = candidate.get("num_turns")
        except json.JSONDecodeError:
            stop_reason = None

        normalized = "".join(ch for ch in (stop_reason or "").lower() if ch.isalnum())
        looks_like_max_turns = "maxturn" in normalized or normalized in (
            "maxturns",
            "maxturnsreached",
            "maxturnsexceeded",
            "turnlimit",
            "maxturnlimit",
            "maxstepsreached",
        )
        highlights = {
            "stopReason": stop_reason,
            "numTurns": num_turns,
            "maxTurns": 1,
            "looksLikeMaxTurnsToken": looks_like_max_turns,
            "latencySeconds": round(duration, 3),
        }
        if stop_reason and looks_like_max_turns:
            detail = "REAL max-turns stop token observed: {!r}".format(stop_reason)
        elif stop_reason:
            detail = "max-turns not cleanly triggered; observed stopReason {!r} (token remains unverified)".format(
                stop_reason
            )
        else:
            detail = "no parseable stopReason; max-turns token remains unverified"
        return ProbeResult("max-turns-token", False, True, command, highlights, detail)
    except (OSError, subprocess.SubprocessError) as exc:
        highlights["error"] = str(exc)
        return ProbeResult(
            "max-turns-token", False, False, command, highlights, "max-turns probe could not run: {}".format(exc)
        )
    finally:
        if home is not None and cwd is not None:
            _destroy_raw_probe_home(home, cwd)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_all_probes() -> List[ProbeResult]:
    """Run every probe in order and return the results (gating and informational)."""
    results: List[ProbeResult] = []
    results.append(probe_preflight())
    results.append(probe_reason_isolated())
    with tempfile.TemporaryDirectory(prefix="grok-skills-live-schema-") as tmp:
        results.append(probe_reason_structured(pathlib.Path(tmp)))
    results.append(probe_reason_web())
    results.append(probe_review())
    results.append(probe_parallel_isolation())
    with tempfile.TemporaryDirectory(prefix="grok-skills-live-code-") as code_tmp:
        results.extend(code_verify_probe.probe_code_and_verify(pathlib.Path(code_tmp)))
    results.append(probe_check_deferral())
    results.append(probe_max_turns_token())
    return results


def _rewrite_pin(installed_version: str) -> Dict[str, object]:
    """Rewrite last-validated stamp (advisory only; never a runtime allowlist)."""
    document = {
        "schemaVersion": 2,
        "enforcement": "none",
        "version": installed_version,
        "validatedAtUtc": _utc_now_iso_z(),
        "probeEvidence": _EVIDENCE_POINTER,
        "note": (
            "Last maintainer-validated Grok CLI build (advisory only). "
            "Runtime does NOT require this exact version."
        ),
    }
    _ACCEPTED_VERSION_FILE.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def _print_summary(results: List[ProbeResult]) -> None:
    print("=" * 72)
    print("Task 13/14 live probe suite")
    print("=" * 72)
    for result in results:
        tag = "PASS" if result.passed else "FAIL"
        kind = "gating" if result.gating else "info  "
        print("[{}] ({}) {}: {}".format(tag, kind, result.name, result.detail))
    gating = [r for r in results if r.gating]
    gating_passed = [r for r in gating if r.passed]
    print("-" * 72)
    print("Gating probes: {}/{} passed".format(len(gating_passed), len(gating)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Task 13 live read-only probe suite for the grok-cli wrapper.")
    parser.add_argument(
        "--revalidate",
        action="store_true",
        help="On a fully green gating run, rewrite accepted-version.json with the installed version.",
    )
    parser.add_argument(
        "--evidence-out",
        default=None,
        help="Optional path to also write the machine-readable evidence JSON.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not _GROK_BINARY.exists():
        sys.stderr.write("[live_probes] grok binary not found at {}\n".format(_GROK_BINARY))
        return 2
    if not _SOURCE_AUTH.is_file():
        sys.stderr.write("[live_probes] auth material not found at {}\n".format(_SOURCE_AUTH))
        return 2

    results = run_all_probes()
    _print_summary(results)

    gating = [r for r in results if r.gating]
    all_gating_green = all(r.passed for r in gating)

    evidence: Dict[str, object] = {
        "suite": "task-13-14-live-probes",
        "generatedAtUtc": _utc_now_iso_z(),
        "installedVersion": _installed_version_first_line(),
        "allGatingGreen": all_gating_green,
        "probes": [r.as_dict() for r in results],
    }

    if args.revalidate:
        if all_gating_green:
            installed = _installed_version_first_line()
            document = _rewrite_pin(installed)
            evidence["pinRewritten"] = True
            evidence["pin"] = document
            print("Revalidation GREEN: accepted-version.json re-pinned to {!r}".format(installed))
        else:
            evidence["pinRewritten"] = False
            print("Revalidation RED: accepted-version.json left untouched (a gating probe failed).")

    if args.evidence_out:
        pathlib.Path(args.evidence_out).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(evidence, indent=2))
    return 0 if all_gating_green else 1


if __name__ == "__main__":
    sys.exit(main())
