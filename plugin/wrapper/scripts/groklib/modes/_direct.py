# wrapper/scripts/groklib/modes/_direct.py
#
# Shared run lifecycle for HARDENED-DIRECT execution (code --integration direct).
# Grok edits the OPERATOR's real repository working tree (no external worktree)
# under a private auth home + OS sandbox write-confined to the repo root + private
# tmp, with secret redaction on output.
#
# SECURITY HONESTY (read before changing this module):
#   - direct mode has NO worktree isolation. Non-protected edits land live in
#     the operator checkout; the operator's git history is the record.
#   - The sandbox confines writes to the repo root (+ private tmp) but does NOT
#     PREVENT writes to .git / .env / keys / hooks INSIDE that root (workspace
#     profile is whole-root). Those paths are snapshotted pre-run and ROLLED
#     BACK post-run on protected-path-write (direct_protect) - detection alone
#     is not enough. Reads of secrets are NOT blocked (D-SECRETREAD gap).
#   - Do NOT claim the sandbox keeps the tree "safe". verify_enforcement proves
#     grant coverage of the writable roots; snapshot/restore + deny scan +
#     write-scope + dirty overlap are the remaining policy layers. Deny scans
#     the full changed-set (incl. gitignored .env); scope + dirty-overlap use
#     source changes only (gitignored byproducts excluded via check-ignore).
#   - Backlog: probe seatbelt write-deny subpaths for true prevention.
#
# Intentionally does NOT reuse run_grok_mode (no finalize hook; read-only single
# phase) and does NOT generalize run_worktree_mode (worktree baked in). Mirrors
# _run_worktree_mode_body's private-home + execute + single-teardown skeleton and
# reuses the _shared/_envelope exports listed below plus WorktreeAccumulator.

import dataclasses
import pathlib
import tempfile
import uuid
from typing import Callable, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import grokcli
from groklib import platformsupport
from groklib import session_store
from groklib.authhome import create_private_home, render_config_toml
from groklib.envelope import build_envelope, failure_envelope
from groklib.progress import ProgressWriter
from groklib.sandbox import policy_for_mode, render_sandbox_toml
from groklib.modes._shared import (
    AUTH_FILE_NAMES,
    AUTH_TEARDOWN_FAILED_ERROR_CLASS,
    HomeCleanup,
    ModeRun,
    _execute_and_verify,
    _policy_field,
    _publish_terminal_envelope,
    _write_prompt_file,
    grok_usage_response_fields,
    resolve_terminal_cleanup,
    source_grok_dir,
    terminalize_unexpected_failure,
)
from groklib.modes._worktree import WorktreeAccumulator
from groklib.modes import direct_protect


def _log(function: str, message: str) -> None:
    log_stderr("modes._direct", function, message)


def _assert_not_linked_worktree(repo_root: pathlib.Path) -> None:
    """Refuse direct mode from a LINKED git worktree (or submodule), where ``.git``
    is a FILE pointing at the common git dir.

    Direct mode's protected snapshot/restore watches ``<repo>/.git`` on disk; in a
    linked worktree the real config/refs/hooks live in the common dir, so a
    ``.git`` write there could be detected but NOT rolled back. Fail closed and
    point the operator at ``--integration worktree`` rather than risk a
    non-restorable ``.git`` mutation.
    """
    git_path = repo_root / ".git"
    try:
        is_file = git_path.is_file()
    except OSError as exc:
        # Fail CLOSED (parity with _assert_state_root_outside_repo): if we cannot
        # even stat .git, we cannot vouch for .git rollback safety.
        raise GrokWrapperError(
            "sandbox-failure",
            "could not classify .git for direct mode: {}".format(exc),
            {"repository": str(repo_root)},
        ) from exc
    if is_file:
        raise GrokWrapperError(
            "sandbox-failure",
            "direct mode is not supported from a linked git worktree / submodule "
            "(.git is a file, so protected .git rollback is not reliable); re-run "
            "with --integration worktree",
            {"repository": str(repo_root)},
        )


def _assert_state_root_outside_repo(repo_root: pathlib.Path) -> None:
    """Fail closed when the run state root is the target repo or nested inside it."""
    try:
        repo = repo_root.resolve()
        state = runstate.state_root().resolve()
    except OSError as exc:
        raise GrokWrapperError(
            "sandbox-failure",
            "could not resolve state root vs repository: {}".format(exc),
        ) from exc
    try:
        state.relative_to(repo)
    except ValueError:
        return  # state root is outside the repo (expected)
    raise GrokWrapperError(
        "sandbox-failure",
        "direct-mode run state root is inside the target repository; set "
        "XDG_STATE_HOME to a path outside the checkout",
        {"stateRoot": str(state), "repository": str(repo)},
    )


@dataclasses.dataclass(frozen=True)
class DirectPrep:
    """What ``prepare`` returns once the prompt is built (no worktree)."""

    cwd: pathlib.Path
    prompt_text: str
    instructions: List[dict]
    tools: Tuple[str, ...]
    # Fingerprint of the operator checkout at run START (tracked + untracked + ignored).
    baseline_fp: "frozenset"
    # Paths already dirty at start (for dirty-path-conflict after re-diff).
    dirty_paths: "frozenset"
    # Sensitive .git/* signatures at start (working-tree fingerprint is blind to .git).
    baseline_git_fp: "frozenset"


@dataclasses.dataclass(frozen=True)
class DirectStage:
    """Context handed to ``prepare``: run identity, progress sink, mutable sinks."""

    run_id: str
    run_paths: runstate.RunPaths
    progress: ProgressWriter
    acc: WorktreeAccumulator


@dataclasses.dataclass(frozen=True)
class DirectFinalizeStage:
    """Context handed to ``finalize`` after Grok returns."""

    result: grokcli.GrokRunResult
    repo_root: pathlib.Path
    effective_model: str
    progress: ProgressWriter
    acc: WorktreeAccumulator
    run_id: str
    run_paths: runstate.RunPaths
    baseline_fp: "frozenset"
    dirty_paths: "frozenset"
    baseline_git_fp: "frozenset"
    force: bool
    # Pre-run protected-path snapshot (bytes under run_dir/protected-snapshot/).
    protect_snapshot: Optional["direct_protect.ProtectedSnapshot"] = None


class DirectHolder:
    """Carries teardown + result out of the body for the outer terminalizer."""

    def __init__(self) -> None:
        self.home_cleanup: Optional[HomeCleanup] = None
        self.result_holder: List[Optional[grokcli.GrokRunResult]] = [None]


def _common_fields(
    *,
    requested_model: str,
    effective_model: Optional[str],
    repository: str,
    target_workspace: Optional[str],
    acc: WorktreeAccumulator,
    tools: Tuple[str, ...],
    web_access: bool,
    instructions: List[dict],
    sandbox_obj: Optional[dict],
    run_paths: runstate.RunPaths,
    cleanup_field: dict,
    warnings: List[str],
) -> dict:
    """Assemble the C4 fields common to direct-mode success and failure envelopes."""
    fields: dict = {
        "requestedModel": requested_model,
        "effectiveModel": effective_model,
        "repository": repository,
        "targetWorkspace": target_workspace,
        "effectiveWorkingDirectory": acc.effective_working_directory or repository,
        "worktreePath": None,
        "worktreeBranch": None,
        "baseRevision": None,
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


def run_direct_mode(
    *,
    binary: pathlib.Path,
    requested_model: str,
    web_access: bool,
    timeout_seconds: int,
    max_turns: Optional[int],
    repository: str,
    target_workspace: Optional[str],
    force: bool,
    prepare: Callable[[DirectStage], DirectPrep],
    finalize: Callable[[DirectFinalizeStage], None],
    session_id: Optional[str] = None,
    seed_session_from_run_dir: Optional[pathlib.Path] = None,
    resume_session: bool = False,
    initial_warnings: Tuple[str, ...] = (),
) -> dict:
    """Execute the hardened-direct lifecycle and return a validated C4 envelope.

    create_run(mode=direct) -> run.json (integration=direct, worktreePath=null) ->
    prepare -> policy_for_mode(direct, repo_root=...) -> private home -> execute +
    verify_enforcement (hard-fail) -> finalize (deny/scope/dirty guards) with a
    guaranteed single private-home teardown. No worktree is ever created.
    """
    mode = "direct"
    acc = WorktreeAccumulator()
    holder = DirectHolder()
    run_paths: Optional[runstate.RunPaths] = None
    progress: Optional[ProgressWriter] = None
    try:
        # Fail closed BEFORE creating the run dir when the state root is nested
        # inside the target repo (e.g. XDG_STATE_HOME=$PWD/.state): direct mode
        # edits the live tree, so run prompt/progress/envelope files landing in
        # the checkout would show up as Grok changes or leak prompt text into
        # commit-able state (mirrors the external-worktree nested-root guard).
        _assert_state_root_outside_repo(pathlib.Path(repository))
        _assert_not_linked_worktree(pathlib.Path(repository))
        run_paths = runstate.create_run(mode)
        progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        return _run_direct_mode_body(
            binary=binary,
            requested_model=requested_model,
            web_access=web_access,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            repository=repository,
            target_workspace=target_workspace,
            force=force,
            prepare=prepare,
            finalize=finalize,
            session_id=session_id,
            seed_session_from_run_dir=seed_session_from_run_dir,
            resume_session=resume_session,
            initial_warnings=initial_warnings,
            run_paths=run_paths,
            progress=progress,
            acc=acc,
            holder=holder,
        )
    except BaseException as exc:
        paths = run_paths if run_paths is not None else getattr(exc, "run_paths", None)
        if paths is None:
            raise
        if progress is None:
            progress = ProgressWriter(paths.run_id, paths.progress_path)
        return terminalize_unexpected_failure(
            run_paths=paths,
            mode=mode,
            progress=progress,
            exc=exc,
            write_terminal_record=lambda: None,
            log=_log,
            cleanup=resolve_terminal_cleanup(holder.home_cleanup),
            result=holder.result_holder[0],
        )


def _run_direct_mode_body(
    *,
    binary: pathlib.Path,
    requested_model: str,
    web_access: bool,
    timeout_seconds: int,
    max_turns: Optional[int],
    repository: str,
    target_workspace: Optional[str],
    force: bool,
    prepare: Callable[[DirectStage], DirectPrep],
    finalize: Callable[[DirectFinalizeStage], None],
    session_id: Optional[str],
    seed_session_from_run_dir: Optional[pathlib.Path],
    resume_session: bool,
    initial_warnings: Tuple[str, ...],
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    acc: WorktreeAccumulator,
    holder: DirectHolder,
) -> dict:
    """Classified-failure body of run_direct_mode (mirrors _run_worktree_mode_body)."""
    mode = "direct"
    repo_root = pathlib.Path(repository)
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
                "worktreePath": None,
                "worktreeBranch": None,
                "baseRevision": None,
                "integration": "direct",
                "status": "running",
            },
        )
    except Exception as exc:
        _log("_run_direct_mode_body", "lifecycle advance failed: {}".format(exc))
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
                    "worktreePath": None,
                    "integration": "direct",
                    "status": "running",
                },
            )
        except Exception as retry_exc:
            _log("_run_direct_mode_body", "retry lifecycle advance failed: {}".format(retry_exc))
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
    progress.safe_emit("start", "direct run created", data={"mode": mode, "integration": "direct"})

    runstate.best_effort_reap_stale_temp_homes(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS)

    warnings: List[str] = list(initial_warnings)
    prep: Optional[DirectPrep] = None
    home_cleanup: Optional[HomeCleanup] = None
    result: Optional[grokcli.GrokRunResult] = None
    result_holder = holder.result_holder
    session_id_holder: List[Optional[str]] = [session_id]
    sandbox_obj: Optional[dict] = None
    effective_model: Optional[str] = None
    outcome_error: Optional[GrokWrapperError] = None
    protect_snapshot: Optional["direct_protect.ProtectedSnapshot"] = None

    try:
        grokcli.check_version(binary)
        progress.safe_emit("validate", "grok version check verified")
        platformsupport.require_probed_platform_for_live()

        stage = DirectStage(
            run_id=run_paths.run_id, run_paths=run_paths, progress=progress, acc=acc
        )
        prep = prepare(stage)
        # Snapshot protected paths BEFORE Grok can write (plant/execute).
        # Workspace sandbox cannot deny .env/.git inside the writable root;
        # finalize restores from this snapshot on protected-path-write.
        protect_snapshot = direct_protect.snapshot_protected_paths(
            repo_root.resolve(), run_paths.run_dir
        )
        progress.safe_emit(
            "worktree",
            "direct mode: operating on real repository tree (no worktree)",
            data={"repository": repository},
        )

        # Two-phase policy (same pattern as _worktree.py:452-475): render
        # sandbox.toml from a placeholder private_tmp, create the private home,
        # then rebuild policy with the run-private tmp for verify_enforcement.
        os_temp = pathlib.Path(tempfile.gettempdir()).resolve()
        toml_policy = policy_for_mode(
            mode, worktree=None, private_tmp=os_temp, repo_root=repo_root.resolve()
        )
        sandbox_toml = render_sandbox_toml(toml_policy, real_home=source_grok_dir().parent)
        home = create_private_home(
            source_grok_dir=source_grok_dir(),
            auth_file_names=AUTH_FILE_NAMES,
            config_toml=render_config_toml(mode=mode),
            sandbox_toml=sandbox_toml,
        )
        home_cleanup = HomeCleanup(home)
        holder.home_cleanup = home_cleanup
        policy = policy_for_mode(
            mode,
            worktree=None,
            private_tmp=grokcli.private_tmp_dir(home).resolve(),
            repo_root=repo_root.resolve(),
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
                elicit_schema=None,
                timeout_seconds=timeout_seconds,
                max_turns=max_turns,
                prompt_text=prep.prompt_text,
                cwd=prep.cwd,
                tools=prep.tools,
                instructions=prep.instructions,
                repository=repository,
                target_workspace=target_workspace,
                detect_unexpected_edits=False,
                session_id=session_id,
                seed_session_from_run_dir=seed_session_from_run_dir,
                resume_session=resume_session,
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
                session_id_holder,
            )
            finalize(
                DirectFinalizeStage(
                    result=result,
                    repo_root=repo_root.resolve(),
                    effective_model=effective_model,
                    progress=progress,
                    acc=acc,
                    run_id=run_paths.run_id,
                    run_paths=run_paths,
                    baseline_fp=prep.baseline_fp,
                    dirty_paths=prep.dirty_paths,
                    baseline_git_fp=prep.baseline_git_fp,
                    force=force,
                    protect_snapshot=protect_snapshot,
                )
            )
        finally:
            try:
                archive_session_id = session_id_holder[0]
                if archive_session_id is None:
                    archive_session_id = str(uuid.uuid4())
                    session_id_holder[0] = archive_session_id
                session_store.archive_session(
                    home.home_dir, run_paths.run_dir, archive_session_id
                )
            except Exception as archive_exc:
                note = "session archive failed (run continues): {}".format(archive_exc)
                _log("_run_direct_mode_body", note)
                acc.warnings.append(note)
            destroy_result = home_cleanup.destroy_once()
            progress.safe_emit(
                "cleanup", "private home destroyed", data={"cleanupStatus": destroy_result["status"]}
            )
    except GrokWrapperError as exc:
        outcome_error = exc
        _log("run_direct_mode", "direct failed: {} ({})".format(exc.error_class, exc))
        # Safety net (reviews 2/3/5): protected-path rollback must also run on an
        # abnormal Grok exit (timeout/cancel/nonzero -> finalize never ran) or a
        # gate/validation/realpath failure inside finalize. protected-path-write
        # already restored, so skip it there to avoid double work.
        if (
            exc.error_class != "protected-path-write"
            and prep is not None
            and protect_snapshot is not None
        ):
            try:
                from groklib.modes import direct_finalize as _direct_finalize
                sweep = _direct_finalize.restore_protected_on_abort(
                    repo_root.resolve(),
                    prep.baseline_fp,
                    prep.baseline_git_fp,
                    protect_snapshot,
                )
                if sweep["restored"]:
                    warnings.append(
                        "protected paths rolled back after abnormal exit: {}".format(
                            sweep["restored"]
                        )
                    )
                if sweep["unrestored"]:
                    warnings.append(
                        "protected paths NOT rolled back (restore them yourself): {}".format(
                            sweep["unrestored"]
                        )
                    )
            except Exception as sweep_exc:
                _log("_run_direct_mode_body", "protected abort-sweep failed: {}".format(sweep_exc))
                warnings.append("protected-path abort-sweep failed: {}".format(sweep_exc))

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
    warnings.extend(acc.warnings)
    cleanup_field = home_result
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
        incomplete = list(result.incomplete_warnings or ())
        fields = _common_fields(
            requested_model=requested_model,
            effective_model=effective_model,
            repository=repository,
            target_workspace=target_workspace,
            acc=acc,
            tools=tools,
            web_access=web_access,
            instructions=instructions,
            sandbox_obj=sandbox_obj,
            run_paths=run_paths,
            cleanup_field=cleanup_field,
            warnings=incomplete + stderr_warnings + warnings,
        )
        fields["grok"] = grok_field
        fields["usage"] = usage_field
        fields["response"] = response_field
        if incomplete:
            # Cancelled/turn-cap with findings: keep response, mark untrustworthy
            # completion (exit_code_for -> 1). SSOT with _success_envelope.
            fields["incompleteStop"] = True
        envelope = build_envelope(run_id=run_paths.run_id, mode=mode, status="success", **fields)
        try:
            record = runstate.load_run_record(run_paths.run_id)
            rev = int(record.get("recordRevision", 0))
            runstate.cas_update_run_record(
                run_paths,
                rev,
                {
                    "requestedModel": requested_model,
                    "repository": repository,
                    "targetWorkspace": target_workspace,
                    "worktreePath": None,
                    "worktreeBranch": None,
                    "baseRevision": None,
                    "integration": "direct",
                    "status": "running",
                },
            )
        except Exception as meta_exc:
            _log("_run_direct_mode_body", "non-terminal metadata merge failed: {}".format(meta_exc))
        return _publish_terminal_envelope(
            run_paths, mode, envelope, lifecycle="completed", progress=progress, warnings=warnings
        )

    error = outcome_error if outcome_error is not None else GrokWrapperError(
        "cli-failure", "direct run did not complete", {"reason": "incomplete-run"}
    )
    fields = _common_fields(
        requested_model=requested_model,
        effective_model=effective_model,
        repository=repository,
        target_workspace=target_workspace,
        acc=acc,
        tools=tools,
        web_access=web_access,
        instructions=instructions,
        sandbox_obj=sandbox_obj,
        run_paths=run_paths,
        cleanup_field=cleanup_field,
        warnings=warnings,
    )
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
        runstate.cas_update_run_record(
            run_paths,
            rev,
            {
                "requestedModel": requested_model,
                "repository": repository,
                "targetWorkspace": target_workspace,
                "worktreePath": None,
                "integration": "direct",
                "status": "running",
            },
        )
    except Exception as meta_exc:
        _log("_run_direct_mode_body", "non-terminal metadata merge failed: {}".format(meta_exc))
    return _publish_terminal_envelope(
        run_paths, mode, envelope, lifecycle="failed", progress=progress, warnings=warnings
    )
