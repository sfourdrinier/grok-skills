# wrapper/scripts/groklib/modes/_worktree.py
#
# Shared run lifecycle for the Grok-spawning WORKTREE-WRITING authority modes
# (code, verify). These share the exact same private-home + execution + single
# teardown skeleton as review/reason (owned by _shared), but they additionally
# run Grok inside an ISOLATED external git worktree and never touch the
# operator's real checkout. The variation between the two modes lives entirely
# in two hooks the caller supplies:
#
#   prepare(stage)  -> WorktreePrep : resolve/create/adopt the worktree, build
#                      the prompt + tool allowlist, and run any pre-Grok
#                      commands (code's optional `pnpm install`, recorded on
#                      stage.acc.commands). It sets stage.holder.worktree the
#                      moment the worktree exists so the runner can retain it
#                      even if a later precondition (verify_external_worktree)
#                      fails and raises.
#   finalize(stage) -> None : the post-Grok checks (code's cwd-sentinel + diff
#                      confinement + build gate; verify's change confinement +
#                      verdict extraction), each of which mutates stage.acc and
#                      fails closed by raising a classified GrokWrapperError.
#
# The runner owns the fail-closed lifecycle: create_run -> run.json ->
# check_version -> prepare -> policy_for_mode(worktree=...) -> private home ->
# execute + model/sandbox verification -> finalize -> single private-home
# teardown in a finally. The private home is ALWAYS destroyed; the C4 `cleanup`
# field describes the WORKTREE disposition instead: code retains its worktree
# (cleanup.status "retained") on every path once it is created, while verify
# never owns the worktree it inspects and so reports the private-home teardown
# result exactly like review/reason. This module writes nothing to stdout.

import dataclasses
import pathlib
import tempfile
from typing import Callable, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import grokcli
from groklib import platformsupport
from groklib.authhome import create_private_home, render_config_toml
from groklib.envelope import build_envelope, failure_envelope
from groklib.progress import ProgressWriter
from groklib.sandbox import policy_for_mode, render_sandbox_toml
from groklib.worktree import ExternalWorktree
from groklib.modes._shared import (
    AUTH_FILE_NAMES,
    AUTH_TEARDOWN_FAILED_ERROR_CLASS,
    HomeCleanup,
    ModeRun,
    _execute_and_verify,
    _policy_field,
    _publish_terminal_envelope,
    _run_record_fields,
    _write_prompt_file,
    grok_usage_response_fields,
    resolve_terminal_cleanup,
    source_grok_dir,
    terminalize_unexpected_failure,
)


def _log(function: str, message: str) -> None:
    log_stderr("modes._worktree", function, message)


@dataclasses.dataclass
class WorktreeAccumulator:
    """Mutable envelope-field sink both hooks write to, read by the runner on every path.

    Kept mutable (not frozen) on purpose: a hook that raises partway through
    (e.g. the build gate failing on its second command, or assert_changes_within
    firing after diff_summary was already recorded) must still surface whatever
    it managed to populate, so the failure envelope carries the commands run and
    the diff observed rather than dropping them.
    """

    commands: List[dict] = dataclasses.field(default_factory=list)
    changed_files: List[str] = dataclasses.field(default_factory=list)
    diff_summary: Optional[str] = None
    effective_working_directory: Optional[str] = None
    verifier: Optional[dict] = None
    # Envelope warnings a finalize hook records (e.g. code mode's D1(b) fail-closed
    # "build gate skipped: gate-scripts-modified"). The runner merges these into the
    # emitted envelope's warnings on both the success and the classified-failure path.
    warnings: List[str] = dataclasses.field(default_factory=list)


class WorktreeHolder:
    """Carries the worktree reference out of ``prepare`` so the runner can retain it on any path.

    ``prepare`` sets ``.worktree`` the instant ``create_external_worktree``
    returns (before ``verify_external_worktree``), so a verify-precondition
    failure still leaves the runner able to record the retained worktree instead
    of losing the reference to a raised exception.
    """

    def __init__(self) -> None:
        self.worktree: Optional[ExternalWorktree] = None
        # Round5 cleanup-outcome-lost-on-terminalize: the body publishes its
        # HomeCleanup here the instant the private home exists, so a raw exception
        # or SIGTERM escaping to run_worktree_mode's outer handler can still surface
        # the private-home teardown outcome (fail-closed on a failed auth teardown)
        # instead of reporting a not-applicable cleanup.
        self.home_cleanup: Optional[HomeCleanup] = None
        # F1-sigterm-drops-result: the body populates this one-element holder the
        # instant grokcli.execute produces a result, so a raw exception or SIGTERM
        # escaping to the outer handler AFTER Grok finished still carries the redacted
        # answer onto the terminal envelope instead of dropping it.
        self.result_holder: List[Optional[grokcli.GrokRunResult]] = [None]


@dataclasses.dataclass(frozen=True)
class WorktreePrep:
    """What ``prepare`` returns once the worktree exists and the prompt is built."""

    worktree: ExternalWorktree
    cwd: pathlib.Path
    prompt_text: str
    instructions: List[dict]
    tools: Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class WorktreeStage:
    """Context handed to ``prepare``: run identity, progress sink, and the mutable sinks."""

    run_id: str
    run_paths: runstate.RunPaths
    progress: ProgressWriter
    acc: WorktreeAccumulator
    holder: WorktreeHolder


@dataclasses.dataclass(frozen=True)
class FinalizeStage:
    """Context handed to ``finalize``: the Grok result, the worktree, and the mutable sinks.

    ``run_id`` is carried so code mode derives its cwd-sentinel name from the SAME
    run id in both ``prepare`` and ``finalize`` (P3), instead of re-deriving it
    from ``worktree.path.name`` in finalize (equal only via the C2 invariant).
    """

    result: grokcli.GrokRunResult
    worktree: ExternalWorktree
    effective_model: str
    progress: ProgressWriter
    acc: WorktreeAccumulator
    run_id: str


def _cleanup_field(worktree_retained: bool, holder: WorktreeHolder, home_result: dict) -> dict:
    """Resolve the C4 cleanup field: retained worktree for code, else the private-home result.

    Once code has created its worktree it is retained on EVERY path (success or
    failure); the wrapper never auto-removes it here (removal is the explicit
    ``cleanup`` subcommand's job). verify never owns the worktree it inspects,
    so it reports the private-home teardown result exactly like review/reason.
    """
    if worktree_retained and holder.worktree is not None:
        return {"status": "retained", "detail": str(holder.worktree.path)}
    return home_result


def _common_fields(
    *,
    requested_model: str,
    effective_model: Optional[str],
    repository: str,
    target_workspace: Optional[str],
    holder: WorktreeHolder,
    acc: WorktreeAccumulator,
    tools: Tuple[str, ...],
    web_access: bool,
    instructions: List[dict],
    sandbox_obj: Optional[dict],
    run_paths: runstate.RunPaths,
    cleanup_field: dict,
    warnings: List[str],
) -> dict:
    """Assemble the C4 fields common to worktree-mode success and failure envelopes."""
    worktree = holder.worktree
    effective_cwd = acc.effective_working_directory
    if effective_cwd is None and worktree is not None:
        effective_cwd = str(worktree.path)
    fields: dict = {
        "requestedModel": requested_model,
        "effectiveModel": effective_model,
        "repository": repository,
        "targetWorkspace": target_workspace,
        "effectiveWorkingDirectory": effective_cwd,
        "worktreePath": str(worktree.path) if worktree is not None else None,
        "worktreeBranch": worktree.branch if worktree is not None else None,
        "baseRevision": worktree.base_revision if worktree is not None else None,
        "policy": _policy_field(tools, web_access),
        "instructions": instructions,
        "changedFiles": list(acc.changed_files),
        "diffSummary": acc.diff_summary,
        "commands": list(acc.commands),
        "progressStreamPath": str(run_paths.progress_path),
        "warnings": warnings,
        "cleanup": cleanup_field,
    }
    if sandbox_obj is not None:
        fields["sandbox"] = sandbox_obj
    if acc.verifier is not None:
        fields["verifier"] = acc.verifier
    return fields


def run_worktree_mode(
    *,
    mode: str,
    binary: pathlib.Path,
    requested_model: str,
    web_access: bool,
    timeout_seconds: int,
    max_turns: int,
    repository: str,
    target_workspace: Optional[str],
    worktree_retained: bool,
    prepare: Callable[[WorktreeStage], WorktreePrep],
    finalize: Callable[[FinalizeStage], None],
    elicit_schema: Optional[dict] = None,
) -> dict:
    """Execute the shared worktree-mode lifecycle for code/verify and return a validated C4 envelope.

    create_run -> run.json -> check_version -> prepare (worktree + prompt) ->
    policy_for_mode(worktree=...) -> private home -> execute + model/sandbox
    verification -> finalize (mode-specific post-Grok checks) with a guaranteed
    single private-home teardown in the finally. Every classified failure is
    returned as a C4 failure envelope; the private home is destroyed on every
    path, and the worktree is retained (code) or left untouched (verify). Any
    UNCLASSIFIED exception that escapes the lifecycle (a raw OSError from
    ``_write_prompt_file``, an envelope-build error) is terminalized under the
    REAL run id via the shared ``terminalize_unexpected_failure`` helper -- a
    terminal run.json is written FIRST, carrying the worktree fields ONLY for the
    run that OWNS the worktree (``worktree_retained``, i.e. code) so cleanup can
    rebuild and reap the physical worktree; a verify run that merely ADOPTS a code
    run's worktree records no worktree, so cleanup of the verify run never tries
    to reap a worktree it does not own. The failure envelope is emitted under that
    SAME run id, never a synthesized one.
    """
    acc = WorktreeAccumulator()
    holder = WorktreeHolder()
    run_paths: Optional[runstate.RunPaths] = None
    progress: Optional[ProgressWriter] = None
    try:
        # create_run is INSIDE the try (F1-create-run-outside-try) so a mid-create
        # failure terminalizes the REAL run rather than orphaning its on-disk dir.
        run_paths = runstate.create_run(mode)
        progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        return _run_worktree_mode_body(
            mode=mode,
            binary=binary,
            requested_model=requested_model,
            web_access=web_access,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            repository=repository,
            target_workspace=target_workspace,
            worktree_retained=worktree_retained,
            prepare=prepare,
            finalize=finalize,
            elicit_schema=elicit_schema,
            run_paths=run_paths,
            progress=progress,
            acc=acc,
            holder=holder,
        )
    except BaseException as exc:  # last-resort: terminalize under the REAL run id
        # BaseException (not just Exception): a SIGTERM-driven SystemExit or a
        # KeyboardInterrupt mid-run still terminalizes the run, tears down the
        # private home (inner finally already ran), and emits exactly one C4
        # envelope (F5-sigterm-bypasses-envelope).
        paths = run_paths if run_paths is not None else getattr(exc, "run_paths", None)
        if paths is None:
            raise
        if progress is None:
            progress = ProgressWriter(paths.run_id, paths.progress_path)
        def _merge_worktree_meta() -> None:
            # Non-terminal metadata only — must not set status success/failure.
            # Always include repository/target so cleanup can rebuild the worktree
            # even when the earlier running-record CAS never landed.
            wt = holder.worktree if worktree_retained else None
            if wt is None:
                return
            record = runstate.load_run_record(paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.cas_update_run_record(
                paths,
                rev,
                {
                    "repository": repository,
                    "targetWorkspace": target_workspace,
                    "worktreePath": str(wt.path),
                    "worktreeBranch": wt.branch,
                    "baseRevision": wt.base_revision,
                    "status": "running",
                },
            )

        return terminalize_unexpected_failure(
            run_paths=paths,
            mode=mode,
            progress=progress,
            exc=exc,
            write_terminal_record=_merge_worktree_meta,
            log=_log,
            cleanup=resolve_terminal_cleanup(holder.home_cleanup),
            result=holder.result_holder[0],
        )


def _run_worktree_mode_body(
    *,
    mode: str,
    binary: pathlib.Path,
    requested_model: str,
    web_access: bool,
    timeout_seconds: int,
    max_turns: int,
    repository: str,
    target_workspace: Optional[str],
    worktree_retained: bool,
    prepare: Callable[["WorktreeStage"], "WorktreePrep"],
    finalize: Callable[["FinalizeStage"], None],
    elicit_schema: Optional[dict],
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    acc: "WorktreeAccumulator",
    holder: "WorktreeHolder",
) -> dict:
    """The classified-failure lifecycle body of run_worktree_mode (see its docstring)."""
    try:
        record = runstate.load_run_record(run_paths.run_id)
        rev = int(record.get("recordRevision", 0))
        if record.get("lifecycle") == "created":
            record = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(record["recordRevision"])
        runstate.cas_update_run_record(
            run_paths,
            rev,
            {
                "requestedModel": requested_model,
                "repository": repository,
                "targetWorkspace": target_workspace,
                "status": "running",
            },
        )
    except Exception as exc:
        _log("_run_worktree_mode_body", "lifecycle advance failed: {}".format(exc))
        try:
            record = runstate.load_run_record(run_paths.run_id)
            rev = int(record.get("recordRevision", 0))
            if record.get("lifecycle") == "created":
                record = runstate.set_lifecycle(run_paths, rev, "running")
                rev = int(record["recordRevision"])
            runstate.cas_update_run_record(
                run_paths,
                rev,
                {
                    "requestedModel": requested_model,
                    "repository": repository,
                    "targetWorkspace": target_workspace,
                    "status": "running",
                },
            )
        except Exception as retry_exc:
            _log("_run_worktree_mode_body", "retry lifecycle advance failed: {}".format(retry_exc))
            raise GrokWrapperError(
                "state-ownership-violation",
                "could not advance run record to running after retry: {}".format(retry_exc),
                {
                    "reason": "run-record-cas-failed",
                    "runId": run_paths.run_id,
                    "firstError": str(exc),
                    "retryError": str(retry_exc),
                },
            ) from retry_exc
    progress.safe_emit("start", "{} run created".format(mode), data={"mode": mode})

    # Reap a crashed prior run's stranded credential-bearing private home on live
    # START (Grok dogfood-2 #1/#7), safely windowed so an active run is never hit.
    runstate.best_effort_reap_stale_temp_homes(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS)

    warnings: List[str] = []
    prep: Optional[WorktreePrep] = None
    home_cleanup: Optional[HomeCleanup] = None
    result: Optional[grokcli.GrokRunResult] = None
    # F1-execute-and-verify-drops-result / F1-sigterm-drops-result: the holder-owned
    # one-element list is populated the instant grokcli.execute produces a result, so
    # a post-run failure (finalize's cwd-sentinel / diff confinement / build gate /
    # verdict, or sandbox/model verification) OR a SIGTERM escaping to the outer
    # handler still carries the completed redacted answer on the failure envelope.
    result_holder = holder.result_holder
    sandbox_obj: Optional[dict] = None
    effective_model: Optional[str] = None
    outcome_error: Optional[GrokWrapperError] = None

    try:
        grokcli.check_version(binary)
        progress.safe_emit("validate", "grok version check verified")

        # Pre-spawn SECURITY GUARANTEE (D-PORT / SEC1): fail closed on any
        # platform without a captured Grok sandbox probe report BEFORE the
        # worktree or private home is created and BEFORE Grok is ever spawned.
        platformsupport.require_probed_platform_for_live()

        stage = WorktreeStage(
            run_id=run_paths.run_id, run_paths=run_paths, progress=progress, acc=acc, holder=holder
        )
        prep = prepare(stage)
        holder.worktree = prep.worktree
        progress.safe_emit("worktree", "worktree ready", data={"worktree": str(prep.worktree.path)})

        # The sandbox profile is deterministic from the mode, so render the
        # sandbox.toml from a policy built with the OS temp dir as a placeholder
        # (render_sandbox_toml ignores writable_roots). The write-confinement
        # policy actually used for verification is rebuilt below with the NARROW
        # run-private tmp once the home exists.
        os_temp = pathlib.Path(tempfile.gettempdir()).resolve()
        toml_policy = policy_for_mode(mode, worktree=prep.worktree.path, private_tmp=os_temp)
        sandbox_toml = render_sandbox_toml(toml_policy, real_home=source_grok_dir().parent)
        home = create_private_home(
            source_grok_dir=source_grok_dir(),
            auth_file_names=AUTH_FILE_NAMES,
            config_toml=render_config_toml(mode=mode),
            sandbox_toml=sandbox_toml,
        )
        # Arm the destroy guard IMMEDIATELY after the home exists, before any
        # progress.emit that could raise (SEC4), so the finally is always armed.
        home_cleanup = HomeCleanup(home)
        # Publish it to the outer terminalizer at once (Round5): a raw exception or
        # SIGTERM escaping after this point must still surface the teardown outcome.
        holder.home_cleanup = home_cleanup
        # Grok dogfood-2 #4: bind write confinement to the RUN-PRIVATE tmp the
        # child actually uses (<home>/tmp), NOT the whole OS temp dir. The broad
        # $TMPDIR was our code passing too wide a root; Grok's own mandatory
        # session-temp union (platformsupport.mandatory_session_temp_roots) still
        # covers the run-private tmp for the M2 subset check, so this only NARROWS
        # the write-confinement surface -- it never loosens it.
        policy = policy_for_mode(
            mode, worktree=prep.worktree.path, private_tmp=grokcli.private_tmp_dir(home).resolve()
        )

        try:
            progress.safe_emit("authhome", "private home created", data={"home": str(home.home_dir)})
            prompt_path = _write_prompt_file(run_paths, prep.prompt_text)
            mode_run = ModeRun(
                mode=mode,
                binary=binary,
                requested_model=requested_model,
                web_access=web_access,
                output_schema=None,
                elicit_schema=elicit_schema,
                timeout_seconds=timeout_seconds,
                max_turns=max_turns,
                prompt_text=prep.prompt_text,
                cwd=prep.cwd,
                tools=prep.tools,
                instructions=prep.instructions,
                repository=repository,
                target_workspace=target_workspace,
                detect_unexpected_edits=False,
            )
            result, sandbox_obj, effective_model = _execute_and_verify(
                mode_run,
                home,
                policy,
                prompt_path,
                run_paths,
                progress,
                result_holder,
                acc.warnings,
            )
            finalize(
                FinalizeStage(
                    result=result,
                    worktree=prep.worktree,
                    effective_model=effective_model,
                    progress=progress,
                    acc=acc,
                    run_id=run_paths.run_id,
                )
            )
        finally:
            destroy_result = home_cleanup.destroy_once()
            progress.safe_emit(
                "cleanup", "private home destroyed", data={"cleanupStatus": destroy_result["status"]}
            )
    except GrokWrapperError as exc:
        outcome_error = exc
        _log("run_worktree_mode", "{} failed: {} ({})".format(mode, exc.error_class, exc))
    # F3: a non-classified failure (e.g. an OSError from _write_prompt_file) after
    # the worktree exists is NOT caught here -- it propagates to run_worktree_mode's
    # outer handler, which terminalizes under the REAL run id (writing a terminal
    # run.json carrying holder.worktree FIRST, so cleanup can rebuild and reap the
    # physical worktree, branch, and marker) and emits the failure envelope under
    # that SAME run id, instead of re-raising into a synthesized entrypoint id.

    # S4 / Grok dogfood-2 #2: a FAILED auth-material teardown is FAIL-CLOSED. Retry
    # once (transient failures), then surface it as a warning AND, when the run
    # would otherwise have succeeded, flip the outcome to a classified
    # cleanup-failure so a possibly-remaining auth copy is a non-success outcome.
    if home_cleanup is not None and home_cleanup.result.get("status") == "failed":
        home_cleanup.retry_if_failed()
    home_result = (
        home_cleanup.result if home_cleanup is not None else {"status": "not-applicable", "detail": None}
    )
    if home_result.get("status") == "failed":
        warnings.append("private home teardown reported failed: {}".format(home_result.get("detail")))
        if outcome_error is None:
            outcome_error = GrokWrapperError(
                AUTH_TEARDOWN_FAILED_ERROR_CLASS,
                "the private home auth-material teardown could not be confirmed clean",
                {"reason": "auth-teardown-failed", "cleanupStatus": "failed"},
            )
    # Merge any warnings a finalize hook recorded (e.g. code mode's D1(b)
    # fail-closed "build gate skipped: gate-scripts-modified") into the envelope
    # warnings, so BOTH the success and the classified-failure envelope surface them.
    warnings.extend(acc.warnings)
    cleanup_field = _cleanup_field(worktree_retained, holder, home_result)
    # Recover the produced result even when _execute_and_verify or finalize raised
    # AFTER grokcli.execute (F1-execute-and-verify-drops-result): on the success
    # path result_holder[0] equals the local `result`; on a post-run failure the
    # tuple assignment never ran, so the local is None but the holder has the
    # completed answer.
    result = result_holder[0]
    instructions = prep.instructions if prep is not None else []
    tools = prep.tools if prep is not None else ()

    if (
        outcome_error is None
        and result is not None
        and sandbox_obj is not None
        and effective_model is not None
    ):
        grok_field, usage_field, response_field, stderr_warnings = grok_usage_response_fields(result)
        # No progress "done" until _publish_terminal_envelope confirms durable_ok.
        fields = _common_fields(
            requested_model=requested_model,
            effective_model=effective_model,
            repository=repository,
            target_workspace=target_workspace,
            holder=holder,
            acc=acc,
            tools=tools,
            web_access=web_access,
            instructions=instructions,
            sandbox_obj=sandbox_obj,
            run_paths=run_paths,
            cleanup_field=cleanup_field,
            warnings=stderr_warnings + warnings,
        )
        fields["grok"] = grok_field
        fields["usage"] = usage_field
        fields["response"] = response_field
        envelope = build_envelope(run_id=run_paths.run_id, mode=mode, status="success", **fields)
        # Non-terminal metadata only (worktree paths); never terminalize without envelope
        try:
            record = runstate.load_run_record(run_paths.run_id)
            rev = int(record.get("recordRevision", 0))
            wt = holder.worktree if worktree_retained else None
            runstate.cas_update_run_record(
                run_paths,
                rev,
                {
                    "requestedModel": requested_model,
                    "repository": repository,
                    "targetWorkspace": target_workspace,
                    "worktreePath": str(wt.path) if wt is not None else None,
                    "worktreeBranch": wt.branch if wt is not None else None,
                    "baseRevision": wt.base_revision if wt is not None else None,
                    "status": "running",
                },
            )
        except Exception as meta_exc:
            _log("_run_worktree_mode_body", "non-terminal metadata merge failed: {}".format(meta_exc))
        return _publish_terminal_envelope(
            run_paths, mode, envelope, lifecycle="completed", progress=progress, warnings=warnings
        )

    error = outcome_error if outcome_error is not None else GrokWrapperError(
        "cli-failure", "{} run did not complete".format(mode), {"reason": "incomplete-run"}
    )
    fields = _common_fields(
        requested_model=requested_model,
        effective_model=effective_model,
        repository=repository,
        target_workspace=target_workspace,
        holder=holder,
        acc=acc,
        tools=tools,
        web_access=web_access,
        instructions=instructions,
        sandbox_obj=sandbox_obj,
        run_paths=run_paths,
        cleanup_field=cleanup_field,
        warnings=warnings,
    )
    # Grok dogfood-3 #4: a POST-run failure (finalize's cwd-sentinel / diff
    # confinement / build gate raised, or an auth-teardown cleanup-failure) must
    # not DROP a completed Grok answer and force recovery from the unredacted
    # progress.jsonl. When the run produced a result, carry the SAME redacted
    # grok/usage/response the success envelope would onto the failure envelope.
    if result is not None:
        grok_field, usage_field, response_field, stderr_warnings = grok_usage_response_fields(result)
        fields["grok"] = grok_field
        fields["usage"] = usage_field
        fields["response"] = response_field
        fields["warnings"] = stderr_warnings + warnings
    envelope = failure_envelope(
        run_id=run_paths.run_id,
        mode=mode,
        error_class=error.error_class,
        message=str(error),
        detail=error.detail or None,
        **fields,
    )
    try:
        record = runstate.load_run_record(run_paths.run_id)
        rev = int(record.get("recordRevision", 0))
        wt = holder.worktree if worktree_retained else None
        runstate.cas_update_run_record(
            run_paths,
            rev,
            {
                "requestedModel": requested_model,
                "repository": repository,
                "targetWorkspace": target_workspace,
                "worktreePath": str(wt.path) if wt is not None else None,
                "worktreeBranch": wt.branch if wt is not None else None,
                "baseRevision": wt.base_revision if wt is not None else None,
                "status": "running",
            },
        )
    except Exception as meta_exc:
        _log("_run_worktree_mode_body", "non-terminal metadata merge failed: {}".format(meta_exc))
    return _publish_terminal_envelope(
        run_paths, mode, envelope, lifecycle="failed", progress=progress, warnings=warnings
    )
