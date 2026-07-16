# wrapper/scripts/groklib/modes/_envelope.py
#
# Envelope-assembly and terminal-failure lifecycle helpers shared by the
# Grok-spawning authority modes. Extracted from modes/_shared.py (900-line cap)
# as a cohesive, self-contained group: the run-lifecycle primitives every mode
# threads through the envelope (the ModeRun request record, the HomeCleanup
# single-teardown guard, the tool/policy field), the single source that builds
# the grok/usage/response C4 sub-objects (so their shape never drifts between the
# review/reason and code/verify lifecycles), the success/failure envelope
# builders, and the last-resort ``terminalize_unexpected_failure`` handler.
#
# This module imports ONLY groklib base modules (never modes/_shared or any other
# mode module), so the dependency is strictly one-directional (_shared -> here);
# _shared re-exports these names for the mode modules and tests that reference
# them via ``_shared.X``. Fail closed on every unverifiable step; this module
# never writes to stdout (envelope.emit from the entrypoint is the sole writer).

import dataclasses
import pathlib
from typing import Callable, List, Optional, Tuple

from groklib import GrokWrapperError, runstate
from groklib import grokcli
from groklib.authhome import PrivateHome, destroy_private_home
from groklib.citations import citations_for_run
from groklib.envelope import (
    InvalidEnvelopeError,
    SecretMaterialError,
    build_envelope,
    failure_envelope,
    redact_secret_material,
    redact_secret_value_text,
)
from groklib.progress import ProgressWriter


class HomeCleanup:
    """Destroys a private home at most once, caching (and never clobbering) the first result.

    The finally block of every mode run calls ``destroy_once``; because a
    second ``destroy_private_home`` on an already-destroyed home reports
    ``failed`` by design (it can no longer enumerate the auth material to
    confirm removal), the cache guarantees a repeat call returns the original
    ``clean`` result instead of overwriting it.
    """

    def __init__(self, home: PrivateHome) -> None:
        self._home: PrivateHome = home
        self._result: Optional[dict] = None

    def destroy_once(self) -> dict:
        if self._result is not None:
            return self._result
        self._result = destroy_private_home(self._home)
        return self._result

    def retry_if_failed(self) -> dict:
        """Retry teardown ONCE when the first destroy reported ``failed``; return the best result.

        Auth-material teardown is fail-closed (S4 / Grok dogfood-2 #2): a run may
        not be reported success while a copy of the operator's Grok auth material
        might still be on disk. Before giving up we retry the destroy exactly
        once, since the first failure may have been transient (a momentarily
        sticky handle). Only a clean retry upgrades the cached result; a second
        ``failed`` (an already-destroyed home reports failed by design) never
        clobbers a prior clean.
        """
        if self._result is None or self._result.get("status") != "failed":
            return self.result
        retry_result = destroy_private_home(self._home)
        if retry_result.get("status") == "clean":
            self._result = retry_result
        return self.result

    @property
    def result(self) -> dict:
        if self._result is not None:
            return self._result
        return {"status": "not-applicable", "detail": None}


# The C4 error class for a private-home auth-material teardown that could not be
# confirmed clean. Reuses the existing "cleanup-failure" registry entry -- the
# SAME class preflight already raises for an unclean probe-home teardown
# (modes/preflight._check_home_and_login / _check_inspect) -- so the fail-closed
# teardown outcome is classified consistently across every mode without widening
# the normative C4 error-class registry.
AUTH_TEARDOWN_FAILED_ERROR_CLASS = "cleanup-failure"


@dataclasses.dataclass(frozen=True)
class ModeRun:
    mode: str
    binary: pathlib.Path
    requested_model: str
    web_access: bool
    output_schema: Optional[dict]
    timeout_seconds: int
    max_turns: Optional[int]
    prompt_text: str
    cwd: pathlib.Path
    tools: Tuple[str, ...]
    instructions: List[dict]
    repository: Optional[str]
    target_workspace: Optional[str]
    detect_unexpected_edits: bool
    extra_temp_dirs: Tuple[pathlib.Path, ...] = ()
    # Elicit-only structured-output schema (verify): sends `--json-schema` to the
    # CLI without engaging grokcli's structured-output validation, so the mode
    # keeps its own missing/invalid classification.
    elicit_schema: Optional[dict] = None


def effective_tools(tools: Tuple[str, ...], web_access: bool) -> List[str]:
    """The tool allowlist actually passed to Grok, delegating to grokcli (T5: one source)."""
    return grokcli.effective_tools(tools, web_access)


def _policy_field(tools: Tuple[str, ...], web_access: bool) -> dict:
    return {
        "tools": effective_tools(tools, web_access),
        "permissionMode": grokcli.HEADLESS_PERMISSION_MODE,
        "subagents": False,
        "webAccess": web_access,
        "memory": False,
    }


def grok_usage_response_fields(
    result: grokcli.GrokRunResult,
) -> Tuple[dict, dict, dict, List[str]]:
    """Build the shared grok/usage/response C4 envelope fields plus the stderr warning lines.

    The single source both _success_envelope (review/reason) and the worktree
    runner (code/verify) use to assemble these three envelope sub-objects, so the
    shape can never drift between the two lifecycles (T6).
    """
    parsed = result.parsed if isinstance(result.parsed, dict) else {}
    raw_usage = parsed.get("usage")
    num_turns = parsed.get("num_turns")
    turns = num_turns if isinstance(num_turns, int) and not isinstance(num_turns, bool) else None
    # Grok's stderr is operator-facing warning text that can quote secret-shaped
    # material (a bearer token, an AWS/GitHub/Slack key, a PEM block) it read from
    # a repo file. Redact the WHOLE stderr text BEFORE splitting into per-line
    # warnings (Round5 pem-truncated-body-leak (b) / Grok dogfood-3 #6): a
    # MULTI-LINE PEM private-key block spans several lines, so redacting each line
    # in isolation never matches the whole-block regex and every body line would
    # leak. Whole-text redaction collapses the block to its placeholder first;
    # splitlines then yields the safe lines. Single-line secrets are still masked.
    # The scanner runs afterwards inside build_envelope as the last-resort backstop.
    stderr_warnings = [
        line for line in redact_secret_value_text(result.stderr or "").splitlines() if line.strip()
    ]
    grok_field = {
        "sessionId": result.session_id,
        "requestId": result.request_id,
        "stopReason": result.stop_reason,
        "modelUsage": result.model_usage,
    }
    usage_field = {"turns": turns, "raw": raw_usage if isinstance(raw_usage, dict) else None}
    # Grok's OWN answer (final_text / structured) can legitimately QUOTE a
    # secret-shaped string it read from a repo file (a review that cites an .env
    # bearer token, a PEM, an sk-/xai- key). Redact it the SAME way stderr is
    # redacted (Grok dogfood-2 #3), so build_envelope's fail-closed scanner does
    # not hard-fail the whole run into a generic validation-failure that loses the
    # body: the secret is masked and the answer STILL reports. The scanner still
    # runs afterwards inside build_envelope as the last-resort backstop.
    redacted_text = (
        redact_secret_value_text(result.final_text) if isinstance(result.final_text, str) else result.final_text
    )
    redacted_structured = redact_secret_material(result.structured) if result.structured is not None else None
    response_field = {
        "text": redacted_text,
        "structured": redacted_structured,
        "stopReason": result.stop_reason,
    }
    return grok_field, usage_field, response_field, stderr_warnings


def _success_envelope(
    run: ModeRun,
    run_paths: runstate.RunPaths,
    result: grokcli.GrokRunResult,
    sandbox_obj: dict,
    effective_model: str,
    cleanup_field: dict,
    warnings: List[str],
) -> dict:
    grok_field, usage_field, response_field, stderr_warnings = grok_usage_response_fields(result)
    citation_list, citation_warnings = citations_for_run(
        web_access=run.web_access,
        final_text=result.final_text if isinstance(result.final_text, str) else None,
    )
    fields: dict = {
        "requestedModel": run.requested_model,
        "effectiveModel": effective_model,
        "repository": run.repository,
        "targetWorkspace": run.target_workspace,
        "effectiveWorkingDirectory": str(run.cwd),
        "sandbox": sandbox_obj,
        "policy": _policy_field(run.tools, run.web_access),
        "instructions": run.instructions,
        "grok": grok_field,
        "usage": usage_field,
        "response": response_field,
        "progressStreamPath": str(run_paths.progress_path),
        "warnings": stderr_warnings + warnings + citation_warnings,
        "cleanup": cleanup_field,
    }
    if citation_list:
        fields["citations"] = citation_list
    return build_envelope(
        run_id=run_paths.run_id,
        mode=run.mode,
        status="success",
        **fields,
    )


def _failure_envelope(
    run: ModeRun,
    run_paths: runstate.RunPaths,
    exc: GrokWrapperError,
    sandbox_obj: Optional[dict],
    effective_model: Optional[str],
    cleanup_field: dict,
    warnings: List[str],
    result: Optional[grokcli.GrokRunResult] = None,
) -> dict:
    fields: dict = {
        "requestedModel": run.requested_model,
        "effectiveModel": effective_model,
        "repository": run.repository,
        "targetWorkspace": run.target_workspace,
        "effectiveWorkingDirectory": str(run.cwd),
        "policy": _policy_field(run.tools, run.web_access),
        "instructions": run.instructions,
        "progressStreamPath": str(run_paths.progress_path),
        "warnings": warnings,
        "cleanup": cleanup_field,
    }
    if sandbox_obj is not None:
        fields["sandbox"] = sandbox_obj
    # Grok dogfood-3 #4: a POST-run failure (unexpected-edits, an auth-teardown
    # cleanup-failure) must not DROP a completed Grok answer, forcing the operator
    # to recover it from the unredacted progress.jsonl. When the run produced a
    # result, carry the SAME redacted grok/usage/response fields the success
    # envelope would, so the safe stdout surface still holds the (redacted) payload.
    if result is not None:
        grok_field, usage_field, response_field, stderr_warnings = grok_usage_response_fields(result)
        fields["grok"] = grok_field
        fields["usage"] = usage_field
        fields["response"] = response_field
        fields["warnings"] = stderr_warnings + warnings
    return failure_envelope(
        run_id=run_paths.run_id,
        mode=run.mode,
        error_class=exc.error_class,
        message=str(exc),
        detail=exc.detail or None,
        **fields,
    )


def resolve_terminal_cleanup(home_cleanup: Optional[HomeCleanup]) -> dict:
    """Resolve the private-home teardown outcome to thread into ``terminalize_unexpected_failure``.

    Round5 cleanup-outcome-lost-on-terminalize: when a raw (non-GrokWrapperError)
    exception or a SIGTERM escapes a mode body AFTER the private home was created,
    the inner ``finally: destroy_once()`` still ran and computed a real teardown
    outcome -- possibly ``failed`` (a copy of the operator's Grok auth material
    might still be on disk). That local never reached the outer terminalizer, so
    the terminal envelope silently reported cleanup ``not-applicable``. The outer
    handler now passes the body's HomeCleanup here to recover the REAL outcome,
    retrying a failed teardown once exactly as the classified path does, so a
    failed auth-material teardown is surfaced FAIL-CLOSED, never as not-applicable.
    """
    if home_cleanup is None:
        return {"status": "not-applicable", "detail": None}
    if home_cleanup.result.get("status") == "failed":
        home_cleanup.retry_if_failed()
    return home_cleanup.result


def terminalize_unexpected_failure(
    *,
    run_paths: runstate.RunPaths,
    mode: str,
    progress: ProgressWriter,
    exc: BaseException,
    write_terminal_record: Callable[[], None],
    log: Callable[[str, str], None],
    cleanup: Optional[dict] = None,
    result: Optional[grokcli.GrokRunResult] = None,
) -> dict:
    """Terminalize a run whose lifecycle threw an UNCLASSIFIED exception, under the REAL run id.

    The single shared last-resort handler for review/reason (``run_grok_mode``),
    preflight, and code/verify (``run_worktree_mode``): on ANY exception that
    escapes the mode's own classified-failure handling (a raw OSError from
    ``_write_prompt_file``, an ``InvalidEnvelopeError``/``SecretMaterialError``
    from the final envelope build, etc.), it writes a terminal ``run.json`` under
    the REAL run id via ``write_terminal_record`` and returns a failure envelope
    under THAT SAME run id -- so the entrypoint never has to synthesize a fresh,
    dangling run id for the emitted envelope (round3 F2/F3; Grok dogfood #2), and
    ``run.json`` never stays stuck at status="running".

    A ``GrokWrapperError`` keeps its own class/message/detail; any other
    exception is classified ``cli-failure``. The ``run.json`` write and the
    progress emit are best-effort (a secondary failure there must not stop the
    envelope from being returned under the real run id), and if the failure
    envelope build itself raises (a secret in the classified detail, a
    programmer error), a minimal detail-free failure envelope is emitted as the
    final fallback so exactly one envelope always reaches the entrypoint.
    """
    if isinstance(exc, GrokWrapperError):
        error_class = exc.error_class
        message = str(exc)
        detail: Optional[dict] = exc.detail or None
    elif isinstance(exc, (SystemExit, KeyboardInterrupt)):
        # A SIGTERM-driven SystemExit or a Ctrl-C KeyboardInterrupt is an external
        # cancellation, not a wrapper bug: classify it as "cancelled" so the
        # terminal envelope is honest (F5-sigterm-bypasses-envelope).
        error_class = "cancelled"
        message = "{} run was cancelled by an external signal".format(mode)
        detail = {"reason": "external-termination", "exceptionType": type(exc).__name__}
    else:
        error_class = "cli-failure"
        message = "{} run failed with an unexpected error".format(mode)
        detail = {"reason": "unexpected-error", "exceptionType": type(exc).__name__}

    log(
        "terminalize_unexpected_failure",
        "{} run {} terminalized after an unclassified failure ({}): {}".format(
            mode, run_paths.run_id, error_class, exc
        ),
    )

    try:
        write_terminal_record()
    except Exception as record_exc:  # best-effort: still emit under the real id
        log(
            "terminalize_unexpected_failure",
            "could not write terminal run.json for {}: {}".format(run_paths.run_id, record_exc),
        )

    try:
        progress.safe_emit("done", "{} failed: {}".format(mode, error_class), level="error")
    except Exception as emit_exc:  # progress is best-effort; never block the envelope
        log(
            "terminalize_unexpected_failure",
            "progress emit failed during terminalization of {}: {}".format(run_paths.run_id, emit_exc),
        )

    # Round5 cleanup-outcome-lost-on-terminalize: carry the REAL private-home teardown
    # outcome onto the terminal envelope so a FAILED auth-material teardown on this
    # raw-exception / SIGTERM path is surfaced FAIL-CLOSED (not the default
    # not-applicable). The cleanup field is ALWAYS-safe and is carried on BOTH the
    # primary and the minimal-fallback envelope; the result-derived fields (which could
    # in the worst case trip the secret scanner) ride ONLY the primary attempt.
    cleanup_fields: dict = {"cleanup": cleanup} if cleanup is not None else {}
    if cleanup is not None and cleanup.get("status") == "failed":
        log(
            "terminalize_unexpected_failure",
            "{} terminalized with a FAILED private-home teardown (auth copy may remain): {}".format(
                run_paths.run_id, cleanup.get("detail")
            ),
        )

    # F1-sigterm-drops-result: a SIGTERM / BaseException that escapes AFTER Grok produced
    # a completed answer (during teardown, no-repo-writes assertion, temp cleanup) must
    # NOT drop that answer and force recovery from the UNREDACTED progress.jsonl. When a
    # result is threaded in, carry the SAME redacted grok/usage/response fields.
    primary_fields: dict = dict(cleanup_fields)
    if result is not None:
        grok_field, usage_field, response_field, stderr_warnings = grok_usage_response_fields(result)
        primary_fields["grok"] = grok_field
        primary_fields["usage"] = usage_field
        primary_fields["response"] = response_field
        if stderr_warnings:
            primary_fields["warnings"] = stderr_warnings

    try:
        return failure_envelope(
            run_id=run_paths.run_id,
            mode=mode,
            error_class=error_class,
            message=message,
            detail=detail,
            **primary_fields,
        )
    except (InvalidEnvelopeError, SecretMaterialError) as build_exc:
        # The classified detail or the carried result could not be embedded safely
        # (a secret in it, or a malformed field). Emit a minimal, detail-free failure
        # envelope under the SAME real run id so exactly one envelope still reaches
        # stdout -- dropping the result-derived fields but still carrying the
        # fail-closed cleanup outcome so a leaked auth copy is never hidden either.
        log(
            "terminalize_unexpected_failure",
            "failure envelope build failed for {}; emitting minimal envelope: {}".format(
                run_paths.run_id, build_exc
            ),
        )
        return failure_envelope(
            run_id=run_paths.run_id,
            mode=mode,
            error_class="cli-failure",
            message="{} run failed and its failure detail could not be safely rendered".format(mode),
            **cleanup_fields,
        )
