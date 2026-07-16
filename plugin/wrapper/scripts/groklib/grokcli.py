# wrapper/scripts/groklib/grokcli.py
#
# C6 Grok CLI execution layer: the single module that actually spawns the
# Grok binary, enforces the wall-clock timeout, and classifies every outcome
# into a C4 error class. Its correctness IS the point of the module, so every
# design choice here is pinned by Task 0 probe evidence (probe-report.md) and
# the C6 invocation baseline, never guessed.
#
# Responsibilities:
#   - accepted_version / check_version: ensure the installed Grok CLI runs
#     and reports a version line (any working build). accepted-version.json
#     is last-validated maintainer evidence only — not a runtime allowlist.
#   - build_argv: emit the EXACT C6 baseline argv (every flag once, no silent
#     additions), delivering the prompt via --prompt-file (never the
#     positional/-p/--single form) and honoring the D-WEB web-access toggle.
#   - build_child_env: a MINIMAL constructed child env (HOME/PATH/TMPDIR
#     only), never an os.environ passthrough, with GROK_SANDBOX unset so the
#     --sandbox flag governs.
#   - execute: spawn via subprocess.Popen in its own process group
#     (platformsupport.spawn_kwargs_new_group), enforce the timeout via
#     communicate(timeout=...), and on TimeoutExpired kill the whole process
#     tree via platformsupport.kill_process_tree (D-PORT: NO raw os.killpg /
#     start_new_session in this module). Classify timeout, cli-failure,
#     output-missing, output-malformed, schema-mismatch, cancelled, and
#     turn-exhaustion; return a GrokRunResult only on a clean success.
#   - the read-only `grok inspect --json` / `grok models` probes (inspect_home /
#     probe_login) now live in groklib.grokcli_probe, reusing this module's
#     minimal-child-env + active-process-registry helpers (900-line cap split).
#
# stdout discipline: this module NEVER writes to stdout (the only stdout
# writer in the package is envelope.emit_envelope). All diagnostics go to
# stderr through the shared log helper. The child process writes to its own
# captured stdout pipe, which this module reads but never re-emits.

import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import signal
import subprocess
import threading
import time
from typing import Dict, List, Iterator, Optional

from groklib import GrokWrapperError, log_stderr
from groklib import grokcli_output
from groklib import grokstream
from groklib import platformsupport
from groklib.authhome import PrivateHome
from groklib.envelope import redact_secret_value_text
from groklib.grokcli_version import ACCEPTED_VERSION_FILE, accepted_version, check_version
from groklib.progress import ProgressWriter
from groklib.sandbox import SandboxPolicy

# C6 version-pin enforcement (accepted_version / check_version / ACCEPTED_VERSION_FILE)
# lives in groklib.grokcli_version and is re-exported here so callers keep using
# grokcli.check_version(...); the split keeps this module under the 900-line cap.
__all__ = ["accepted_version", "check_version", "ACCEPTED_VERSION_FILE"]

# Probe-pinned web tool identifiers (Decision Log D-WEB; live probe, isolated
# home, web_search exercised successfully). When web_access is True these are
# appended to the mode's tool allowlist and --disable-web-search is omitted.
WEB_TOOLS = ("web_search", "web_fetch", "open_page", "open_page_with_find")

# Task 0 pin (probe-report.md Steps 4-5): the only permission mode under which
# allowlisted mutating tools execute headlessly without interaction while
# tools outside the allowlist are hard-denied. review/reason are read-only so
# the value is uniform across modes; this constant documents the pin.
HEADLESS_PERMISSION_MODE = "auto"

# Task 0 built-in tool inventory (probe-report.md Step 5). When a mode's
# effective tool allowlist is empty, C6 requires --tools be omitted AND every
# built-in denied via --disallowed-tools, so a run with no allowlist cannot
# silently inherit any built-in tool.
ALL_BUILTIN_TOOLS = (
    "x_user_search",
    "x_semantic_search",
    "x_keyword_search",
    "x_thread_fetch",
    "run_terminal_command",
    "read_file",
    "search_replace",
    "list_dir",
    "grep",
    "kill_command_or_subagent",
    "todo_write",
    "get_command_or_subagent_output",
    "spawn_subagent",
    "scheduler_create",
    "scheduler_delete",
    "scheduler_list",
    "monitor",
    "search_tool",
    "use_tool",
    "update_goal",
    "enter_plan_mode",
    "exit_plan_mode",
    "ask_user_question",
    "image_gen",
    "image_edit",
    "image_to_video",
    "reference_to_video",
    "write",
)

# The complete set of flags build_argv may emit. Used by the wrapper's own
# tests to assert no flag outside the C6 baseline is ever added. --allow is
# intentionally NOT here: Task 0 proved --allow rules are unnecessary under
# --permission-mode auto, and C6 does not list it in the baseline, so
# GrokRunSpec.allow_rules is recorded on the spec for the envelope's policy
# surface but never placed in the child argv (no silent additions, C6).
C6_BASELINE_FLAGS = frozenset(
    {
        "--prompt-file",
        "--verbatim",
        "--cwd",
        "--model",
        "--output-format",
        "--json-schema",
        "--permission-mode",
        "--tools",
        "--disallowed-tools",
        "--no-subagents",
        "--no-memory",
        "--disable-web-search",
        "--no-plan",
        "--sandbox",
        "--max-turns",
        "--session-id",
        "--leader-socket",
    }
)

_INSPECT_TIMEOUT_SECONDS = 60
_MODELS_TIMEOUT_SECONDS = 60
# Grace period to reap a child after a process-group kill; SIGKILL is
# immediate on POSIX, so this only guards a pathological non-exit.
_REAP_TIMEOUT_SECONDS = 5

_PRIVATE_TMP_DIRNAME = "tmp"


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "grokcli" component prefix."""
    log_stderr("grokcli", function, message)


# Registry of currently-running Grok child processes, each spawned in its OWN
# session/process group (platformsupport.spawn_kwargs_new_group). The entrypoint's
# SIGTERM handler (grok_agent) calls terminate_active_processes so an external
# termination (e.g. the plugin stop-review gate group-killing the wrapper on its
# own timeout) tears the grok CLI down instead of orphaning it in its escaped
# session. Guarded by a lock because register/unregister run on the main thread
# while the signal handler may fire between them.
_ACTIVE_PROCS: "set[subprocess.Popen]" = set()
_ACTIVE_PROCS_LOCK = threading.Lock()


@contextlib.contextmanager
def _sigterm_blocked() -> "Iterator[None]":
    """Block SIGTERM delivery to the calling (main) thread for the duration of the block.

    Closes the TOCTOU window between ``subprocess.Popen`` returning a live,
    credential-bearing Grok child (spawned in its OWN process group) and its
    registration in ``_ACTIVE_PROCS`` (F1-signal-race-orphan-grok-child). If the
    entrypoint's SIGTERM handler ran in that window it would snapshot an empty
    registry and orphan the child. Blocking SIGTERM across spawn+register means a
    SIGTERM arriving mid-spawn is merely DEFERRED: once the child is registered
    and the mask is restored, the pending signal is delivered and
    ``terminate_active_processes`` sees (and kills) the child. POSIX-only
    (``pthread_sigmask``); a no-op elsewhere and on any masking failure (the run
    proceeds unblocked rather than crashing).
    """
    if not platformsupport.is_posix() or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    except (OSError, ValueError) as exc:
        _log("_sigterm_blocked", "could not block SIGTERM around spawn (continuing): {}".format(exc))
        yield
        return
    try:
        yield
    finally:
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        except (OSError, ValueError) as exc:
            _log("_sigterm_blocked", "could not unblock SIGTERM after spawn: {}".format(exc))


def _register_active_proc(proc: "subprocess.Popen") -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.add(proc)


def _unregister_active_proc(proc: "subprocess.Popen") -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.discard(proc)


def terminate_active_processes() -> int:
    """Force-kill every currently-running Grok child process tree; return how many were signaled.

    Called from the entrypoint's SIGTERM handler so a gate/harness termination
    reaches the grok CLI the wrapper spawned in its own session and does not leave
    it running (consuming API quota, holding a copy of the operator's credentials
    via the private home). Best-effort: a per-process kill failure is logged and
    the sweep continues, and this never raises out of a signal handler.
    """
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        try:
            platformsupport.kill_process_tree(proc)
        except Exception as exc:  # best-effort teardown; never raise from a signal handler
            _log("terminate_active_processes", "failed killing grok process tree: {}".format(exc))
    return len(procs)


@dataclasses.dataclass(frozen=True)
class GrokRunSpec:
    binary: pathlib.Path
    cwd: pathlib.Path
    model: str
    prompt_file: pathlib.Path
    output_schema: "dict|None"
    tools: "tuple[str, ...]"
    allow_rules: "tuple[str, ...]"
    sandbox: SandboxPolicy
    permission_mode: str
    max_turns: "int|None"
    timeout_seconds: int
    leader_socket: pathlib.Path
    session_id: str
    subagents_enabled: bool
    web_access: bool
    home: PrivateHome
    # Elicit-only structured-output schema: sends `--json-schema` to the CLI so
    # the model reliably returns structured output, WITHOUT engaging this
    # module's structured-output validation / missing-output classification.
    # Used by verify, which owns its own verifier-unavailable classification for
    # a missing or invalid verdict (spec 5.5). Mutually exclusive with
    # output_schema (which both elicits AND validates).
    elicit_schema: "dict|None" = None


@dataclasses.dataclass(frozen=True)
class GrokRunResult:
    argv: "tuple[str, ...]"
    exit_status: int
    stdout: str
    stderr: str
    duration_seconds: float
    parsed: "dict|None"
    stop_reason: "str|None"
    session_id: "str|None"
    request_id: "str|None"
    model_usage: "dict|None"
    effective_model: "str|None"
    final_text: "str|None"
    structured: "object|None"
    # Operator-visible incomplete-stop notes (Cancelled/turn-cap salvage).
    incomplete_warnings: "tuple[str, ...]" = ()


def effective_tools(tools: "tuple[str, ...]", web_access: bool) -> List[str]:
    """Resolve a mode's tool allowlist, appending WEB_TOOLS when web access is enabled (D-WEB).

    The single source of tool-allowlist expansion, shared by build_argv (via
    _effective_tools) and the modes' policy field (modes._shared.effective_tools
    delegates here).
    """
    resolved = list(tools)
    if web_access:
        resolved = resolved + list(WEB_TOOLS)
    return resolved


def _effective_tools(spec: GrokRunSpec) -> List[str]:
    """Resolve the spec's tool allowlist (delegates to the shared ``effective_tools``)."""
    return effective_tools(spec.tools, spec.web_access)


def build_argv(spec: GrokRunSpec) -> List[str]:
    """Build the EXACT C6 baseline argv for one Grok run.

    Every C6 baseline flag appears exactly once, in C6 order, with no silent
    additions. The prompt is delivered via ``--prompt-file`` (never the
    positional / ``-p`` / ``--single`` single-turn form). ``--output-format
    streaming-json`` is ALWAYS emitted (decision D-STREAM / T2-0): the single
    output-format switch that lets execute relay Grok's live thought/text tokens
    into progress.jsonl while still assembling the same final result. A schema
    run additionally emits ``--json-schema`` (T2-0.0 probe: the two flags
    compose, and the terminal ``end`` event still carries ``structuredOutput``).
    An empty effective tool allowlist omits ``--tools`` and denies every built-in
    via ``--disallowed-tools`` (C6). ``--no-subagents`` is present whenever
    subagents are disabled (always, in v1). Web access (D-WEB) omits
    ``--disable-web-search`` and folds WEB_TOOLS into the allowlist.
    """
    argv: List[str] = [str(spec.binary)]

    argv.extend(["--prompt-file", str(spec.prompt_file)])
    argv.append("--verbatim")
    argv.extend(["--cwd", str(spec.cwd)])
    argv.extend(["--model", spec.model])

    # D-STREAM (T2-0): always stream. Grounded by the T2-0.0 live probe, this is
    # an output-format switch ONLY - no sandbox/permission/tool/socket/home flag
    # changes. A schema run (output_schema = elicit+validate, elicit_schema =
    # elicit only; mutually exclusive per mode) ALSO emits --json-schema, which
    # the probe confirmed composes with streaming-json (structuredOutput arrives
    # on the terminal event).
    argv.extend(["--output-format", "streaming-json"])
    cli_schema = spec.output_schema if spec.output_schema is not None else spec.elicit_schema
    if cli_schema is not None:
        argv.extend(["--json-schema", json.dumps(cli_schema, sort_keys=True)])

    argv.extend(["--permission-mode", spec.permission_mode])

    tools = _effective_tools(spec)
    if tools:
        argv.extend(["--tools", ",".join(tools)])
    else:
        argv.extend(["--disallowed-tools", ",".join(ALL_BUILTIN_TOOLS)])

    if not spec.subagents_enabled:
        argv.append("--no-subagents")

    argv.append("--no-memory")

    if not spec.web_access:
        argv.append("--disable-web-search")

    argv.append("--no-plan")

    argv.extend(["--sandbox", spec.sandbox.profile])
    # Optional: only pass --max-turns when the operator set an explicit budget.
    # Default is unlimited (no flag) so long reviews are not artificial-capped.
    if spec.max_turns is not None:
        argv.extend(["--max-turns", str(spec.max_turns)])
    argv.extend(["--session-id", spec.session_id])
    argv.extend(["--leader-socket", str(spec.leader_socket)])

    return argv


def _reduced_path(binary: pathlib.Path) -> str:
    """Build a PATH reduced to the directories needed to locate grok and git (C6).

    Contains the grok binary's own directory, the git binary's directory (as
    discovered on the operator's PATH), and the OS default helper directories
    (``os.defpath``, a Python constant, not an ``os.environ`` passthrough) so
    git's own subprocesses and the child interpreter resolve. Deduplicated,
    order-preserving.
    """
    ordered: List[str] = []
    seen = set()

    def _add(directory: str) -> None:
        if directory and directory not in seen:
            seen.add(directory)
            ordered.append(directory)

    _add(str(binary.parent))
    git_path = shutil.which("git")
    if git_path:
        _add(str(pathlib.Path(git_path).parent))
    for default_dir in os.defpath.split(os.pathsep):
        _add(default_dir)

    return os.pathsep.join(ordered)


def private_tmp_dir(home: PrivateHome) -> pathlib.Path:
    """The run-private TMPDIR the grok child is given (``<home>/tmp``).

    The single source of the child's temporary-directory path, shared by
    ``_minimal_env`` (which sets ``TMPDIR``) and the mode runners (which pass it
    as the sandbox's narrow writable root instead of the whole OS temp dir,
    Grok dogfood-2 #4). Keeping one definition means the write-confinement root
    can never drift from the directory the child actually writes temp files into.
    """
    return home.home_dir / _PRIVATE_TMP_DIRNAME


def _minimal_env(home: PrivateHome, binary: pathlib.Path) -> Dict[str, str]:
    """Construct the minimal C6 child env: exactly HOME, PATH, TMPDIR; GROK_SANDBOX unset."""
    return {
        "HOME": str(home.home_dir),
        "PATH": _reduced_path(binary),
        "TMPDIR": str(private_tmp_dir(home)),
    }


def build_child_env(spec: GrokRunSpec) -> Dict[str, str]:
    """Build the minimal C6 child env for a full Grok run (HOME/PATH/TMPDIR only)."""
    return _minimal_env(spec.home, spec.binary)


def _ensure_private_tmp(tmp_dir: pathlib.Path) -> None:
    """Create the run-private TMPDIR (owner-only), failing closed as cli-failure on error."""
    try:
        tmp_dir.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        _log("_ensure_private_tmp", "could not create private tmp dir: {}".format(exc))
        raise GrokWrapperError(
            "cli-failure",
            "could not create the run-private temporary directory",
            {"reason": "private-tmp-failed"},
        )
    platformsupport.restrict_dir_permissions(tmp_dir)


def _reap_after_kill(proc: "subprocess.Popen") -> None:
    """Reap a child after a process-group kill; never raises out of the timeout path."""
    try:
        proc.communicate(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _log("_reap_after_kill", "child did not exit after group kill: {}".format(exc))
        try:
            proc.kill()
            proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as inner:
            _log("_reap_after_kill", "final reap fallback failed: {}".format(inner))


class _StreamOutcome:
    """The raw outcome of consuming Grok's streaming-json stdout (pre-classification).

    Carries the assembled result (via ``assembler``), the drained stderr, the
    concatenated raw stdout (for the result record only, never re-parsed), and
    the two fail-closed flags the caller classifies: ``timed_out`` (the watchdog
    killed the tree) and ``malformed`` (a non-JSON / non-object line appeared).
    """

    def __init__(
        self,
        assembler: grokstream.StreamAssembler,
        stderr_text: str,
        raw_stdout: str,
        timed_out: bool,
        malformed: bool,
    ) -> None:
        self.assembler = assembler
        self.stderr_text = stderr_text
        self.raw_stdout = raw_stdout
        self.timed_out = timed_out
        self.malformed = malformed


def _relay_stream(proc: "subprocess.Popen", spec: GrokRunSpec, progress: ProgressWriter) -> _StreamOutcome:
    """Read Grok's streaming-json stdout line by line, relaying each event to progress AS IT ARRIVES.

    A background ``threading.Timer`` watchdog enforces the wall-clock timeout by
    killing the WHOLE process tree via platformsupport.kill_process_tree (the
    same D-PORT mechanism the pre-D-STREAM path used, unchanged); a background
    thread drains stderr so its pipe cannot fill and deadlock the child. The main
    thread owns the read loop and is the ONLY caller of ``progress.emit`` for the
    lifetime of this function (the watchdog and stderr threads never emit), so
    the ProgressWriter's single-writer monotonic-seq guarantee is preserved. A
    non-JSON / non-object line is recorded (``malformed``) and the stream is
    still drained to natural EOF so the child exits cleanly and is reaped by the
    caller; the caller classifies it as output-malformed.
    """
    assembler = grokstream.StreamAssembler()
    coalescer = grokstream.ProgressCoalescer()
    stderr_parts: List[str] = []
    stdout_parts: List[str] = []
    malformed = False
    # F-STREAM-OSERR: a progress.jsonl append failure (OSError) MUST NOT crash
    # the run. The single source of truth for that degrade is
    # ProgressWriter.safe_emit (it flips the writer degraded on the first OSError
    # and no-ops after), used both here and across the whole run lifecycle, so
    # the read loop keeps draining Grok's stdout, the assembler still builds the
    # final result, and execute() still returns a GrokRunResult.
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        _log("_relay_stream", "grok run exceeded {}s timeout; killing process tree".format(spec.timeout_seconds))
        platformsupport.kill_process_tree(proc)
        # Guarantee the main-thread read loop unblocks and the run terminates even
        # if the process-group kill was swallowed (killpg EPERM / an already
        # reparented tree). kill_process_tree logs-and-swallows its failures, so
        # without this a pathological child could leave proc.stdout.readline
        # blocked forever and the wall-clock timeout would never return. Direct
        # -kill the leader pid, then close stdout to force EOF; the read loop
        # tolerates the resulting close/read error and exits.
        try:
            proc.kill()
        except OSError as exc:
            _log("_on_timeout", "direct-kill fallback failed after timeout: {}".format(exc))
        stdout = proc.stdout
        if stdout is not None:
            try:
                stdout.close()
            except (OSError, ValueError) as exc:
                _log("_on_timeout", "closing grok stdout to force EOF failed: {}".format(exc))

    def _drain_stderr() -> None:
        try:
            for chunk in iter(lambda: proc.stderr.read(4096), ""):
                stderr_parts.append(chunk)
        except (OSError, ValueError) as exc:
            _log("_drain_stderr", "error draining grok stderr: {}".format(exc))

    def _safe_emit(message: str, *, data: Optional[Dict[str, object]] = None) -> None:
        """Emit one relay progress event, degrading (not crashing) on a progress-write OSError."""
        progress.safe_emit("grok", message, data=data)

    def _emit(payload: Optional[Dict[str, object]]) -> None:
        if payload is not None:
            _safe_emit("grok streamed {} tokens".format(payload.get("event")), data=payload)

    watchdog = threading.Timer(spec.timeout_seconds, _on_timeout)
    watchdog.daemon = True
    stderr_thread = threading.Thread(target=_drain_stderr, name="grok-stderr-drain")
    stderr_thread.daemon = True
    watchdog.start()
    stderr_thread.start()
    try:
        for raw_line in iter(proc.stdout.readline, ""):
            if not raw_line.strip():
                continue
            stdout_parts.append(raw_line)
            obj = grokstream.try_parse_stream_line(raw_line)
            if obj is None:
                malformed = True
                continue
            event = assembler.feed(obj)
            if event.kind in ("thought", "text"):
                _emit(coalescer.feed(event.kind, event.text))
            else:
                _emit(coalescer.flush())
                if event.kind == "other":
                    _safe_emit(
                        "grok streamed an unrecognized event",
                        data={"event": "other", "eventType": event.event_type},
                    )
    except UnicodeDecodeError as exc:
        # F-STREAM-DECODE: proc.stdout is a strict UTF-8 text stream (Popen
        # text=True, encoding="utf-8"), so a non-UTF-8 byte from grok makes
        # readline() raise here. Fail CLOSED as output-malformed (same class as a
        # non-JSON line) instead of letting the raw UnicodeDecodeError escape past
        # execute()'s classification and orphan the real run's envelope/record.
        _log("_relay_stream", "grok stdout had undecodable UTF-8; classifying output-malformed: {}".format(exc))
        malformed = True
    except (OSError, ValueError) as exc:
        # The read pipe was force-closed to unblock a timeout (_on_timeout closes
        # stdout), or the pipe failed. ``timed_out`` governs the classification
        # (timeout) in that case; a genuine non-timeout pipe error falls through
        # to execute()'s empty/torn-stream classification. Either way the loop
        # ends here rather than the raw error escaping execute().
        _log("_relay_stream", "grok stdout read ended abnormally (timed_out={}): {}".format(timed_out.is_set(), exc))
    finally:
        watchdog.cancel()
        stderr_thread.join(timeout=_REAP_TIMEOUT_SECONDS)
        # Close the pipes we drained ourselves (communicate() used to own this);
        # both are at EOF here (stdout loop ended, stderr thread joined), so no
        # thread is still reading them.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError as exc:
                    _log("_relay_stream", "error closing grok pipe: {}".format(exc))
    _emit(coalescer.flush())

    return _StreamOutcome(assembler, "".join(stderr_parts), "".join(stdout_parts), timed_out.is_set(), malformed)


def execute(spec: GrokRunSpec, progress: ProgressWriter) -> GrokRunResult:
    """Spawn Grok, relay its streaming-json events, enforce the timeout, and classify the outcome.

    Returns a ``GrokRunResult`` on a clean success **or** on an incomplete stop
    (Cancelled / operator turn-cap) that still produced usable model output.
    Incomplete successes set ``incomplete_warnings`` (also folded into the mode
    envelope ``warnings``). Hard failure modes raise ``GrokWrapperError``:

      - wall-clock timeout           -> timeout (process TREE killed first)
      - empty stream, exit 0         -> output-missing
      - empty stream, exit != 0      -> cli-failure (stderr captured)
      - non-JSON/non-object line, OR
        a torn stream with no terminal event -> output-malformed
      - stopReason Cancelled without usable output -> cancelled
      - operator max-turn budget exhausted without usable output -> turn-exhaustion
      - any other nonzero exit       -> cli-failure (stderr captured)
      - structured output failing the caller schema -> schema-mismatch (pointer in detail)
        (on incomplete stops with text findings, structured is cleared and a
        warning is recorded instead of raising)

    D-STREAM (T2-0): Grok is run with ``--output-format streaming-json`` and its
    live thought/text tokens are relayed into the run's progress.jsonl AS THEY
    ARRIVE (via _relay_stream), while the final result is assembled from the
    terminal ``end`` event PLUS the concatenated text tokens - equivalent to the
    single ``--output-format json`` blob the pre-D-STREAM path parsed. No
    sandbox/worktree/auth/tool/socket boundary changes; only the output format.

    The child is spawned in its own process group so that on a timeout the WHOLE
    tree is killed via platformsupport.kill_process_tree (D-PORT: no raw
    os.killpg / start_new_session here). stderr on an otherwise-valid run is
    surfaced as a progress warning and carried on the result for the caller to
    fold into the envelope's warnings.
    """
    argv = build_argv(spec)
    env = build_child_env(spec)
    _ensure_private_tmp(pathlib.Path(env["TMPDIR"]))

    progress.safe_emit(
        "grok",
        "spawning grok cli",
        data={"model": spec.model, "sandboxProfile": spec.sandbox.profile, "maxTurns": spec.max_turns},
    )

    start = time.monotonic()
    # Spawn and register ATOMICALLY under a SIGTERM block so a SIGTERM can never
    # orphan a live grok child in the window between Popen and registration
    # (F1-signal-race-orphan-grok-child).
    with _sigterm_blocked():
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=str(spec.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                **platformsupport.spawn_kwargs_new_group(),
            )
        except OSError as exc:
            _log("execute", "could not spawn grok cli: {}".format(exc))
            progress.safe_emit("grok", "could not spawn grok cli", level="error")
            raise GrokWrapperError(
                "cli-failure",
                "could not spawn the grok cli process",
                {"reason": "spawn-failed", "exitStatus": None},
            )
        _register_active_proc(proc)

    try:
        return _relay_and_classify(proc, spec, progress, argv, start)
    finally:
        _unregister_active_proc(proc)


def _relay_and_classify(
    proc: "subprocess.Popen",
    spec: GrokRunSpec,
    progress: ProgressWriter,
    argv: List[str],
    start: float,
) -> GrokRunResult:
    """Relay Grok's stream, reap the child, and classify the outcome into a GrokRunResult or GrokWrapperError.

    Split out of ``execute`` so it can register/unregister the child in the
    active-process registry around this whole body via one try/finally, without
    indenting the entire classification ladder. stderr captured into any
    ``error.detail`` is redacted here (a secret Grok printed to stderr must not
    ride a cli-failure/output-missing envelope to stdout); the fail-closed
    envelope scanner remains the last-resort backstop.
    """
    outcome = _relay_stream(proc, spec, progress)

    if outcome.timed_out:
        # The watchdog already killed the process tree; reap the child and fail
        # closed as timeout (the exact D-PORT kill mechanism, unchanged).
        progress.safe_emit(
            "grok",
            "grok run exceeded timeout; killing process group",
            level="error",
            data={"timeoutSeconds": spec.timeout_seconds},
        )
        _reap_after_kill(proc)
        raise GrokWrapperError(
            "timeout",
            "grok run exceeded its {} second wall-clock timeout".format(spec.timeout_seconds),
            {"timeoutSeconds": spec.timeout_seconds},
        )

    # The stream reached EOF (natural child exit); reap for the exit status.
    try:
        proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _log("execute", "grok did not exit after stream EOF; killing process tree")
        platformsupport.kill_process_tree(proc)
        _reap_after_kill(proc)

    duration = time.monotonic() - start
    exit_status = proc.returncode
    stderr = outcome.stderr_text or ""
    stdout = outcome.raw_stdout or ""

    if outcome.malformed:
        # F-STREAM-MALFORMED: a non-JSON / non-object line appeared. Fail closed
        # as output-malformed regardless of whether any VALID line was also seen.
        # A FULLY-malformed stream never reaches the assembler, so saw_any_line
        # stays False; this MUST be checked BEFORE the empty-stream branch below,
        # or a fully-malformed run (which DID emit output, just unparseable) would
        # be misclassified as output-missing / cli-failure.
        progress.safe_emit(
            "grok", "grok streaming output was malformed", level="error", data={"reason": "unparseable-stream-line"}
        )
        raise GrokWrapperError(
            "output-malformed",
            "grok streaming output was malformed (unparseable-stream-line)",
            {"reason": "unparseable-stream-line", "exitStatus": exit_status},
        )

    if not outcome.assembler.saw_any_line:
        if exit_status != 0:
            progress.safe_emit(
                "grok", "grok cli exited nonzero with no output", level="error", data={"exitStatus": exit_status}
            )
            raise GrokWrapperError(
                "cli-failure",
                "grok cli exited with status {} and produced no output".format(exit_status),
                {"exitStatus": exit_status, "stderr": redact_secret_value_text(stderr)},
            )
        progress.safe_emit("grok", "grok produced no stdout", level="error", data={"exitStatus": exit_status})
        raise GrokWrapperError(
            "output-missing",
            "grok produced no stdout",
            {"exitStatus": exit_status, "stderr": redact_secret_value_text(stderr)},
        )

    if not outcome.assembler.has_terminal:
        # Valid line(s) but no terminal `end` event: a torn/incomplete stream.
        progress.safe_emit(
            "grok", "grok streaming output was malformed", level="error", data={"reason": "no-terminal-event"}
        )
        raise GrokWrapperError(
            "output-malformed",
            "grok streaming output was malformed (no-terminal-event)",
            {"reason": "no-terminal-event", "exitStatus": exit_status},
        )

    parsed = outcome.assembler.build_parsed()
    fields = grokcli_output.extract_result_fields(parsed)
    stop_reason = fields["stop_reason"]
    num_turns = fields["num_turns"]
    structured = fields["structured"]
    usable = grokcli_output.has_usable_model_output(fields)
    incomplete_stop = False
    incomplete_warnings: List[str] = []

    # Turn budget (only when operator set --max-turns). Grok often reports the
    # cap as stopReason "Cancelled"; check turn-exhaustion before plain cancel.
    if grokcli_output.is_turn_exhaustion(stop_reason, num_turns, spec.max_turns):
        if usable:
            incomplete_stop = True
            msg = (
                "grok hit turn budget (stopReason={!r}, numTurns={!r}, maxTurns={!r}) "
                "but produced usable output; findings kept"
            ).format(stop_reason, num_turns, spec.max_turns)
            incomplete_warnings.append(msg)
            progress.safe_emit(
                "grok",
                msg,
                level="warning",
                data={"stopReason": stop_reason, "numTurns": num_turns, "maxTurns": spec.max_turns},
            )
        else:
            progress.safe_emit(
                "grok",
                "grok run exhausted its turn budget",
                level="error",
                data={"stopReason": stop_reason, "numTurns": num_turns, "maxTurns": spec.max_turns},
            )
            raise GrokWrapperError(
                "turn-exhaustion",
                "grok run exhausted its turn budget (stopReason {!r})".format(stop_reason),
                {"stopReason": stop_reason, "numTurns": num_turns, "maxTurns": spec.max_turns},
            )

    elif grokcli_output.is_cancelled(stop_reason):
        # Incomplete stop with content: keep findings (do not response:null).
        if usable:
            incomplete_stop = True
            msg = (
                "grok stopReason={!r} (numTurns={!r}) with usable output; findings kept "
                "(run may be incomplete)"
            ).format(stop_reason, num_turns)
            incomplete_warnings.append(msg)
            progress.safe_emit(
                "grok",
                msg,
                level="warning",
                data={"stopReason": stop_reason, "numTurns": num_turns, "exitStatus": exit_status},
            )
        else:
            progress.safe_emit(
                "grok",
                "grok run cancelled",
                level="error",
                data={"stopReason": stop_reason},
            )
            raise GrokWrapperError(
                "cancelled",
                "grok run was cancelled (stopReason {!r})".format(stop_reason),
                {"stopReason": stop_reason, "exitStatus": exit_status, "numTurns": num_turns},
            )

    elif exit_status != 0:
        # Valid JSON but a nonzero exit that is not a recognized cancelled /
        # turn-exhaustion stop reason: a clean grok run exits 0, so this is a
        # cli-failure with stderr preserved.
        progress.safe_emit("grok", "grok cli exited nonzero", level="error", data={"exitStatus": exit_status})
        raise GrokWrapperError(
            "cli-failure",
            "grok cli exited with status {}".format(exit_status),
            {
                "exitStatus": exit_status,
                "stderr": redact_secret_value_text(stderr),
                "stopReason": stop_reason,
                "numTurns": num_turns,
            },
        )

    if spec.output_schema is not None:
        if structured is None:
            if incomplete_stop and usable:
                msg = (
                    "schema requested but structuredOutput missing on incomplete stop; "
                    "keeping text findings"
                )
                incomplete_warnings.append(msg)
                progress.safe_emit("grok", msg, level="warning")
            else:
                progress.safe_emit("grok", "grok returned no structured output for a schema run", level="error")
                raise GrokWrapperError(
                    "schema-mismatch",
                    "grok returned no structuredOutput for a schema-constrained run",
                    {"pointer": "/", "reason": "structured-output-missing"},
                )
        else:
            try:
                grokcli_output.validate_structured_output(structured, spec.output_schema)
            except GrokWrapperError:
                text_ok = isinstance(fields.get("final_text"), str) and bool(
                    str(fields.get("final_text") or "").strip()
                )
                if incomplete_stop and text_ok:
                    # Do not ship schema-invalid structured on a green incomplete run.
                    structured = None
                    msg = (
                        "structuredOutput failed schema on incomplete stop; "
                        "keeping final text findings only (structured cleared)"
                    )
                    incomplete_warnings.append(msg)
                    progress.safe_emit("grok", msg, level="warning")
                else:
                    raise

    if stderr.strip():
        progress.safe_emit("grok", "grok emitted stderr diagnostics", level="warning")

    progress.safe_emit(
        "grok",
        "grok run completed",
        data={
            "stopReason": stop_reason,
            "numTurns": num_turns,
            "maxTurns": spec.max_turns,
            "durationSeconds": round(duration, 6),
        },
    )

    return GrokRunResult(
        argv=tuple(argv),
        exit_status=exit_status,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        parsed=parsed,
        stop_reason=stop_reason,
        session_id=fields["session_id"],
        request_id=fields["request_id"],
        model_usage=fields["model_usage"],
        effective_model=fields["effective_model"],
        final_text=fields["final_text"],
        structured=structured,
        incomplete_warnings=tuple(incomplete_warnings),
    )
