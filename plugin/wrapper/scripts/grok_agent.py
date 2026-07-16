#!/usr/bin/env python3
# wrapper/scripts/grok_agent.py
#
# Thin operator CLI entrypoint (C8): parse argv into the exact C8 subcommand
# surface, dispatch to the mode registry (groklib.modes.MODES), and print
# EXACTLY ONE C4 envelope to stdout on every path (usage error, classified
# GrokWrapperError, unexpected exception, or a stored-copy write failure),
# exiting 0 iff success. Diagnostics go to stderr; emit_envelope owns stdout.

import argparse
import json
import os
import pathlib
import signal
import sys
import traceback
from typing import List, Optional

from groklib import GrokWrapperError, grokcli, log_stderr, platformsupport, runstate
from groklib.envelope import MODES as CLI_MODES
from groklib.envelope import (
    assert_no_secret_material,
    emit_envelope,
    exit_code_for,
    failure_envelope,
    redact_secret_value_text,
    validate_envelope,
)
from groklib.modes import MODES

_DEFAULT_BINARY = os.path.join("~", ".grok", "bin", "grok")
_NON_STORING_MODES = frozenset({"status", "cleanup"})
_SIGTERM_EXIT_CODE = 143  # 128 + SIGTERM, the conventional signal-terminated code

# Fail-closed upper bounds for the operator-supplied run budgets (Grok r3 #11
# timeout-max-turns-not-validated): a non-positive or absurd value is rejected at
# argv parse as a cheap usage-error, instead of being accepted and then burning a
# full private-home create/destroy cycle (a --timeout <= 0 fires the watchdog the
# instant the child spawns). The ceilings are generous (7 days / 100k turns) so no
# realistic run is ever refused; they only bound obviously-nonsense input. The
# --timeout ceiling is the SAME constant the stale-home reaper uses for its
# unknown-lease hard cap (runstate.MAX_RUN_TIMEOUT_SECONDS), so the argv clamp and
# the reap cap can never drift (Grok r5 #5).
_MAX_TIMEOUT_SECONDS = runstate.MAX_RUN_TIMEOUT_SECONDS
_MAX_TURNS = 100_000


def _bounded_positive_int(flag: str, maximum: int):
    """Return an argparse ``type`` that accepts an integer in [1, ``maximum``], else usage-error.

    A rejection raises argparse.ArgumentTypeError, which the subparser's overridden
    error() turns into a classified ``usage-error`` C4 envelope (fail closed at the
    boundary, exactly like every other malformed input in this wrapper).
    """

    def _parse(raw: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("{} expects an integer, got {!r}".format(flag, raw))
        if value < 1 or value > maximum:
            raise argparse.ArgumentTypeError(
                "{} must be an integer between 1 and {}, got {}".format(flag, maximum, value)
            )
        return value

    return _parse


def _install_sigterm_handler() -> None:
    """Install a SIGTERM handler (POSIX) that tears down any running grok child, then exits.

    A gate/harness that group-kills this wrapper on its own timeout (the plugin
    stop-review gate) must not orphan the grok CLI the wrapper spawned in its own
    session. On SIGTERM we force-kill every active grok process tree
    (grokcli.terminate_active_processes) and raise SystemExit, so the lifecycle
    finally-blocks (private-home teardown) still run rather than being skipped by
    a default terminate. Best-effort: only installed on POSIX and only when
    signal registration succeeds (it requires the main thread); a failure leaves
    the process on the default disposition rather than crashing.

    Intentional envelope-over-signal exit code (Grok dogfood-3 #9): the
    SystemExit(143) raised here is CAUGHT by the mode lifecycle's ``except
    BaseException`` and turned into a terminal ``cancelled`` C4 envelope, after
    which ``main`` exits with ``exit_code_for(envelope)`` (1), NOT 143. This is a
    DELIBERATE contract, not a lost signal: the wrapper's invariant is that every
    run emits EXACTLY ONE C4 envelope to stdout and exits with that envelope's
    status-derived code, so an observer parses the machine-readable ``cancelled``
    outcome (with the honest cleanup field) rather than inferring cancellation
    from a raw 143. Preserving 143 would require bypassing the envelope emit on
    this path, which would break the one-envelope guarantee; the 143 is emitted on
    stderr context instead. Callers that need signal semantics read the envelope.
    """
    if not platformsupport.is_posix():
        return

    def _handler(_signum: int, _frame: object) -> None:
        killed = grokcli.terminate_active_processes()
        log_stderr(
            "grok_agent",
            "_sigterm",
            "SIGTERM received; killed {} grok process tree(s); exiting".format(killed),
        )
        raise SystemExit(_SIGTERM_EXIT_CODE)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError) as exc:
        log_stderr(
            "grok_agent",
            "_install_sigterm_handler",
            "could not install SIGTERM handler (continuing without it): {}".format(exc),
        )


class _UsageError(Exception):
    """Raised in place of argparse's exit(2) so usage errors become C4 envelopes."""

    def __init__(self, message: str, mode: Optional[str]) -> None:
        super().__init__(message)
        self.usage_message = message
        self.mode = mode


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose error() raises _UsageError instead of printing and exiting 2."""

    mode_label: Optional[str] = None

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message, self.mode_label)


def resolve_binary() -> pathlib.Path:
    """Resolve the grok binary: GROK_AGENT_BINARY override (test/operator), else ~/.grok/bin/grok."""
    raw = os.environ.get("GROK_AGENT_BINARY", _DEFAULT_BINARY)
    return pathlib.Path(os.path.expanduser(raw))


def _add_task_group(sub: argparse.ArgumentParser) -> None:
    group = sub.add_mutually_exclusive_group(required=True)
    group.add_argument("--task")
    group.add_argument("--task-file")


def _add_run_opts(sub: argparse.ArgumentParser, *, timeout: int) -> None:
    # No default max-turns: omit the CLI flag unless the operator sets --max-turns.
    # Artificial turn caps discard review findings; Grok CLI subscription runs
    # continue until EndTurn (or explicit timeout / optional --max-turns).
    sub.add_argument("--model", default="grok-4.5")
    sub.add_argument("--timeout", type=_bounded_positive_int("--timeout", _MAX_TIMEOUT_SECONDS), default=timeout)
    sub.add_argument(
        "--max-turns",
        type=_bounded_positive_int("--max-turns", _MAX_TURNS),
        default=None,
        help="Optional Grok turn cap. Default: unlimited (flag omitted).",
    )


def _build_parser() -> _Parser:
    parser = _Parser(prog="grok_agent.py")
    subs = parser.add_subparsers(dest="mode", required=True)

    def _sub(name: str) -> _Parser:
        created = subs.add_parser(name)
        created.mode_label = name
        return created

    _sub("preflight")

    def _add_web_flags(sub: _Parser) -> None:
        # None = use per-mode default table (web_defaults); True/False from flags.
        sub.set_defaults(web=None)
        sub.add_argument("--web", dest="web", action="store_const", const=True)
        sub.add_argument("--no-web", dest="web", action="store_const", const=False)

    review = _sub("review")
    review.add_argument("--target", required=True)
    _add_task_group(review)
    _add_web_flags(review)
    review.add_argument("--schema")
    _add_run_opts(review, timeout=900)

    reason = _sub("reason")
    _add_task_group(reason)
    reason.add_argument("--input", action="append", default=[])
    reason.add_argument("--rules-file", action="append", default=[])
    _add_web_flags(reason)
    reason.add_argument("--schema")
    _add_run_opts(reason, timeout=900)

    code = _sub("code")
    code.add_argument("--target", required=True)
    code.add_argument("--base", required=True)
    _add_task_group(code)
    _add_web_flags(code)
    _add_run_opts(code, timeout=3600)

    verify = _sub("verify")
    verify.add_argument("--worktree", required=True)
    _add_task_group(verify)
    _add_run_opts(verify, timeout=1800)

    status = _sub("status")
    status.add_argument("--run-id", required=True)

    cleanup = _sub("cleanup")
    cleanup.add_argument("--run-id", required=True)
    cleanup.add_argument("--confirm", action="store_true")

    return parser


def _mode_hint(argv: List[str]) -> str:
    """Best-effort mode label for a pre-parse usage error: first known subcommand token, else preflight."""
    for token in argv:
        if token in CLI_MODES:
            return token
    return "preflight"


def _with_store_failure_warning(envelope: dict, exc: Exception) -> dict:
    """Return a shallow copy of ``envelope`` with a stored-write-failure warning appended.

    Preserves the ORIGINAL envelope's status and every field verbatim; only the
    ``warnings`` list gains one honest note that the stored copy could not be
    written. A successful run stays a success.
    """
    reemitted = dict(envelope)
    existing = reemitted.get("warnings")
    warnings = list(existing) if isinstance(existing, list) else []
    # Redact the exception text before it joins a stdout-bound envelope: an OSError
    # can carry a path, and (defensively) any secret-shaped substring is masked so
    # the re-emit re-scan below cannot be tripped by the diagnostic itself.
    warnings.append(
        "stored envelope copy could not be written: {}".format(redact_secret_value_text(str(exc)))
    )
    reemitted["warnings"] = warnings
    return reemitted


def _emit(envelope: dict, envelope_path: Optional[pathlib.Path]) -> int:
    """Emit exactly one stdout envelope, preserving result fidelity on a stored-copy write failure.

    On a stored-copy write failure (which raises BEFORE anything reaches stdout),
    re-emit the SAME ORIGINAL envelope with ``envelope_path=None`` -- preserving
    its real status and fields (a successful run must NOT become a cli-failure) --
    with a warning appended (F2). Only if that no-store re-emit ALSO raises does a
    last-resort minimal cli-failure envelope emit. Exactly one envelope reaches
    stdout on every path.
    """
    try:
        emit_envelope(envelope, envelope_path)
        return exit_code_for(envelope)
    except Exception as exc:  # stored-copy write failed BEFORE stdout was written
        log_stderr(
            "grok_agent",
            "_emit",
            "stored envelope write failed; re-emitting the original envelope without a stored copy: {}".format(exc),
        )
        try:
            reemitted = _with_store_failure_warning(envelope, exc)
            # review-1 #11: preserve the "every stdout envelope was scanned"
            # invariant -- the re-emit adds a new warning, so re-scan before it
            # reaches stdout. A secret in the appended text (already redacted
            # above) or anywhere else fails closed to the minimal envelope below.
            assert_no_secret_material(reemitted)
            emit_envelope(reemitted, None)
            return exit_code_for(reemitted)
        except Exception as inner:  # re-emit failed, or the re-scan (SecretMaterialError) rejected it
            log_stderr(
                "grok_agent",
                "_emit",
                "no-store re-emit of the original envelope also failed; emitting a minimal cli-failure: {}".format(inner),
            )
            fallback = failure_envelope(
                run_id=str(envelope.get("runId") or runstate.new_run_id()),
                mode=str(envelope.get("mode") or "preflight"),
                error_class="cli-failure",
                message="failed to persist the stored envelope copy and to re-emit the original envelope",
            )
            emit_envelope(fallback, None)
            return 1


def main(argv: Optional[List[str]] = None) -> int:
    _install_sigterm_handler()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        env = failure_envelope(
            run_id=runstate.new_run_id(),
            mode=exc.mode or _mode_hint(argv),
            error_class="usage-error",
            message=exc.usage_message,
        )
        return _emit(env, None)

    mode = args.mode
    args.grok_binary = resolve_binary()
    run_fn = MODES.get(mode)
    if run_fn is None:
        env = failure_envelope(
            run_id=runstate.new_run_id(),
            mode=mode,
            error_class="cli-failure",
            message="mode {!r} is not available in this build".format(mode),
        )
        return _emit(env, None)

    try:
        env = run_fn(args)
    except GrokWrapperError as exc:
        log_stderr("grok_agent", "main", "classified failure escaped {}: {} ({})".format(mode, exc.error_class, exc))
        env = failure_envelope(
            run_id=runstate.new_run_id(), mode=mode, error_class=exc.error_class, message=str(exc), detail=exc.detail or None
        )
        return _emit(env, None)
    except Exception as exc:  # unexpected: traceback to stderr, never stdout
        traceback.print_exc(file=sys.stderr)
        log_stderr("grok_agent", "main", "unexpected failure in {}: {}".format(mode, exc))
        env = failure_envelope(
            run_id=runstate.new_run_id(), mode=mode, error_class="cli-failure", message="unexpected wrapper failure: {}".format(exc)
        )
        return _emit(env, None)
    except (SystemExit, KeyboardInterrupt) as exc:
        # F5-baseexception-during-earliest-create-run-window: a SIGTERM-driven
        # SystemExit or a Ctrl-C in the EARLIEST create_run window (before a run
        # dir exists, so the mode runner re-raises it) is a BaseException, NOT an
        # Exception, and would otherwise escape past sys.exit(main()) with NO
        # stdout envelope -- breaking the "exactly one C4 envelope on every path"
        # contract. Terminalize it fail-closed as a classified "cancelled" outcome.
        log_stderr("grok_agent", "main", "{} cancelled by an external signal: {}".format(mode, type(exc).__name__))
        env = failure_envelope(
            run_id=runstate.new_run_id(),
            mode=mode,
            error_class="cancelled",
            message="{} run was cancelled by an external signal before it could start".format(mode),
            detail={"reason": "external-termination", "exceptionType": type(exc).__name__},
        )
        return _emit(env, None)

    # PR1 single terminal writer: modes own durable envelope.json via
    # persist_terminal_envelope. Entrypoint never O_TRUNC-stores a second copy.
    # Keep doNotStore on stdout when present so callers can distinguish ephemeral
    # (stdout-only) failures from durable terminal envelopes (design §9.4).
    return _emit(env, None)


if __name__ == "__main__":
    sys.exit(main())
