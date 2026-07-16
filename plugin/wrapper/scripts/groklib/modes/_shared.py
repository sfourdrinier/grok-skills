# wrapper/scripts/groklib/modes/_shared.py
#
# Shared run lifecycle for the Grok-spawning authority modes (review, reason;
# code and verify reuse this in Task 11). Every such mode follows the exact
# same skeleton: runstate.create_run -> write the C2 run.json record ->
# authhome.create_private_home -> try: check version, execute, verify sandbox
# enforcement, assemble the C4 envelope -> finally: destroy the private home
# EXACTLY ONCE, recording the destroy result in the envelope `cleanup` field.
# The private home is destroyed on every path including failures (spec 7); the
# HomeCleanup guard makes a repeat destroy a cached no-op so a second attempt
# can never clobber a prior "clean" status with the "failed" an
# already-destroyed home reports by design.
#
# This module owns nothing mode-specific: review and reason each prepare their
# own working directory, tool allowlist, C7 rules payload, and instruction
# entries, then hand a ModeRun to run_grok_mode. Fail closed on every
# unverifiable step; the wrapper never writes to stdout here (envelope.emit is
# the sole stdout writer, from the entrypoint).

import argparse
import datetime
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import uuid
from typing import Callable, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import grokcli
from groklib import platformsupport
from groklib.authhome import (
    PrivateHome,
    create_private_home,
    render_config_toml,
)
from groklib.grokcli import GrokRunSpec
from groklib.progress import ProgressWriter
from groklib.sandbox import SandboxPolicy, policy_for_mode, render_sandbox_toml, verify_enforcement
from groklib.worktree import ExternalWorktree
from groklib import worktree_escape
# Envelope-assembly + terminal-failure lifecycle group, extracted to modes/_envelope
# (900-line cap). Re-exported here so the mode modules and tests that reference these
# names as ``_shared.X`` keep resolving through this module's public surface.
from groklib.modes._envelope import (
    AUTH_TEARDOWN_FAILED_ERROR_CLASS,
    HomeCleanup,
    ModeRun,
    _failure_envelope,
    _policy_field,
    _success_envelope,
    effective_tools,
    grok_usage_response_fields,
    resolve_terminal_cleanup,
    terminalize_unexpected_failure,
)
from groklib.modes.finalize_worker import run_finalize_parent

# Task 0 (probe-report.md Step 2): the sole authentication file under ~/.grok.
AUTH_FILE_NAMES: Tuple[str, ...] = ("auth.json",)

# Top-level Grok JSON keys that, when non-empty, prove Grok reported a file
# change. review is read-only (read-only tools + read-only sandbox are the two
# ENFORCED layers); scanning the JSON output for any of these is a third,
# defense-in-depth check so a run that somehow reports an edit fails closed as
# unexpected-edits instead of being reported as a clean review.
_CHANGE_KEYS: Tuple[str, ...] = (
    "changedFiles",
    "fileChanges",
    "filesChanged",
    "modifiedFiles",
    "editedFiles",
    "edits",
)

_GIT_TIMEOUT_SECONDS = 30
_REASON_CWD_PREFIX = "grok-reason-cwd-"


def _log(function: str, message: str) -> None:
    log_stderr("modes._shared", function, message)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (shared C2 timestamp source)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def source_grok_dir() -> pathlib.Path:
    """The real per-user Grok home whose auth material every private home isolates (D-PORT)."""
    return pathlib.Path.home() / ".grok"


def repo_root_for_path(anchor: pathlib.Path) -> pathlib.Path:
    """Resolve the git repo root that CONTAINS ``anchor`` via ``git -C <dir> rev-parse --show-toplevel``.

    Repo-agnostic (standalone grok-skills): the repository under review/edit is
    the one that CONTAINS the resolved --target (or the caller's cwd), NEVER where
    this wrapper happens to be installed. So the caller passes the resolved target
    directory (or a file within the repo, or the cwd) as ``anchor``; a file anchor
    is resolved to its parent directory before the git query. Every confinement
    guard downstream is then computed relative to THIS target-derived root, so
    `code --target /some/other/repo` confines to that repo's own worktree
    regardless of where grok-skills lives. Any git failure (not a repo, git
    absent, empty output) is a fail-closed ``invalid-target``.
    """
    resolved = pathlib.Path(anchor).resolve()
    anchor_dir = resolved if resolved.is_dir() else resolved.parent
    argv = ["git", "-C", str(anchor_dir), "rev-parse", "--show-toplevel"]
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log("repo_root_for_path", "could not run git rev-parse for {}: {}".format(anchor_dir, exc))
        raise GrokWrapperError(
            "invalid-target",
            "could not resolve the repository root from the target path",
            {"reason": "git-toplevel-failed", "anchor": str(anchor_dir)},
        )
    if completed.returncode != 0:
        _log("repo_root_for_path", "git rev-parse exited {} for {}".format(completed.returncode, anchor_dir))
        raise GrokWrapperError(
            "invalid-target",
            "the target path is not inside a git repository: {}".format(anchor_dir),
            {"exitStatus": completed.returncode, "anchor": str(anchor_dir)},
        )
    root = (completed.stdout or "").strip()
    if not root:
        raise GrokWrapperError(
            "invalid-target",
            "git rev-parse returned an empty repository root",
            {"reason": "empty-toplevel", "anchor": str(anchor_dir)},
        )
    return pathlib.Path(root)


def best_effort_write_run_record(
    run_paths: runstate.RunPaths, record: dict, warnings: List[str], log: Callable[[str, str], None]
) -> None:
    """Merge run.json under lock, degrading to a warning (never raising) on failure."""
    try:
        runstate.write_run_record(run_paths, record)
    except Exception as exc:
        log(
            "best_effort_write_run_record",
            "could not persist terminal run.json for {}: {}".format(run_paths.run_id, exc),
        )
        warnings.append("the terminal run record could not be persisted (see stderr for detail)")


def _publish_terminal_envelope(
    run_paths: runstate.RunPaths,
    mode: str,
    envelope: dict,
    *,
    lifecycle: str,
    progress: ProgressWriter,
    warnings: List[str],
) -> dict:
    """Move to finalizing and persist terminal envelope via finalize worker."""
    try:
        record = runstate.load_run_record(run_paths.run_id)
        rev = int(record.get("recordRevision", 0))
        life = record.get("lifecycle")
        if life == "created":
            record = runstate.set_lifecycle(run_paths, rev, "running")
            rev = int(record["recordRevision"])
            life = "running"
        if life == "running":
            record = runstate.set_lifecycle(run_paths, rev, "finalizing")
            rev = int(record["recordRevision"])
        published, ephemeral = run_finalize_parent(
            run_paths,
            mode=mode,
            envelope=envelope,
            lifecycle=lifecycle,
            expected_revision=rev,
            progress=progress,
        )
        if ephemeral:
            warnings.append("finalization worker unkillable; durable terminal state not written")
            if isinstance(published, dict):
                published = dict(published)
                published["warnings"] = list(published.get("warnings") or []) + [
                    "finalization worker unkillable; durable terminal state not written"
                ]
        return published
    except Exception as exc:
        _log("_publish_terminal_envelope", "finalize failed: {}".format(exc))
        note = "terminal finalize failed (see stderr); returning in-memory envelope"
        warnings.append(note)
        repaired = dict(envelope)
        repaired["warnings"] = list(envelope.get("warnings") or []) + [note]
        return repaired


def resolve_binary(args: argparse.Namespace) -> pathlib.Path:
    """Return the grok binary the entrypoint resolved onto ``args``, failing closed if absent."""
    binary = getattr(args, "grok_binary", None)
    if not isinstance(binary, pathlib.Path):
        raise GrokWrapperError(
            "cli-failure",
            "grok binary was not resolved before dispatch",
            {"reason": "missing-grok-binary"},
        )
    return binary


def resolve_task_text(args: argparse.Namespace) -> str:
    """Resolve the task text from --task or --task-file (argparse enforces exactly one)."""
    task = getattr(args, "task", None)
    if isinstance(task, str):
        return task
    task_file = getattr(args, "task_file", None)
    if not isinstance(task_file, str):
        raise GrokWrapperError(
            "usage-error",
            "exactly one of --task or --task-file is required",
            {"reason": "missing-task"},
        )
    path = pathlib.Path(task_file)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _log("resolve_task_text", "could not read task file {}: {}".format(path, exc))
        raise GrokWrapperError(
            "invalid-target",
            "could not read the task file: {}".format(path),
            {"taskFile": str(path)},
        )


def load_output_schema(args: argparse.Namespace) -> Optional[dict]:
    """Load the optional --schema file as a JSON object, failing closed on a malformed schema."""
    schema_path = getattr(args, "schema", None)
    if not isinstance(schema_path, str):
        return None
    path = pathlib.Path(schema_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log("load_output_schema", "could not read schema file {}: {}".format(path, exc))
        raise GrokWrapperError(
            "invalid-target",
            "could not read the schema file: {}".format(path),
            {"schemaFile": str(path)},
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log("load_output_schema", "schema file {} is not valid JSON: {}".format(path, exc))
        raise GrokWrapperError(
            "usage-error",
            "the schema file is not valid JSON: {}".format(path),
            {"schemaFile": str(path)},
        )
    if not isinstance(parsed, dict):
        raise GrokWrapperError(
            "usage-error",
            "the schema file must contain a JSON object",
            {"schemaFile": str(path)},
        )
    return parsed


def make_reason_cwd() -> pathlib.Path:
    """Create a fresh, owner-private temp working directory for reason, OUTSIDE the repo."""
    return pathlib.Path(tempfile.mkdtemp(prefix=_REASON_CWD_PREFIX))


def _write_prompt_file(run_paths: runstate.RunPaths, prompt_text: str) -> pathlib.Path:
    """Write the run-private prompt file (0600) under the run dir and return its path."""
    prompt_path = run_paths.run_dir / "prompt.txt"
    fd = os.open(str(prompt_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(prompt_text)
    return prompt_path


def _run_record_fields(
    *,
    mode: str,
    run_paths: runstate.RunPaths,
    status_value: str,
    requested_model: str,
    repository: Optional[str],
    target_workspace: Optional[str],
    worktree: Optional[ExternalWorktree],
) -> dict:
    """Build the C2 run.json record, populating the worktree fields when a worktree exists."""
    return {
        "schemaVersion": 1,
        "runId": run_paths.run_id,
        "mode": mode,
        "createdAtUtc": utc_now_iso(),
        "status": status_value,
        "requestedModel": requested_model,
        "repository": repository,
        "targetWorkspace": target_workspace,
        "worktreePath": str(worktree.path) if worktree is not None else None,
        "worktreeBranch": worktree.branch if worktree is not None else None,
        "baseRevision": worktree.base_revision if worktree is not None else None,
        "progressStreamPath": str(run_paths.progress_path),
        "envelopePath": str(run_paths.envelope_path),
    }


def _run_record(run: ModeRun, run_paths: runstate.RunPaths, status_value: str) -> dict:
    return _run_record_fields(
        mode=run.mode,
        run_paths=run_paths,
        status_value=status_value,
        requested_model=run.requested_model,
        repository=run.repository,
        target_workspace=run.target_workspace,
        worktree=None,
    )


def _capture_review_fs_baseline(
    run: "ModeRun",
    warnings: Optional[List[str]] = None,
) -> Optional["frozenset"]:
    """Snapshot dirty paths BEFORE a read-only run, for an informational drift note only.

    Only when ``detect_unexpected_edits`` and a repository are set. Capture failures
    never block the review: they append an informational note (when ``warnings`` is
    provided) and return None so Grok still runs. Drift is not a success gate.
    """
    if not run.detect_unexpected_edits or not run.repository:
        return None
    try:
        return worktree_escape.repo_change_fingerprint(pathlib.Path(run.repository))
    except GrokWrapperError as exc:
        msg = (
            "Could not snapshot the tree before this review "
            "(informational only; review continues): {}".format(exc)
        )
        _log(
            "_capture_review_fs_baseline",
            "baseline capture failed (soft-skip drift note): {}".format(exc),
        )
        if warnings is not None:
            warnings.append(msg)
        return None


def _report_repo_fs_drift(
    run: "ModeRun",
    baseline: Optional["frozenset"],
    progress: ProgressWriter,
    warnings: List[str],
) -> None:
    """Note checkout FS drift during a read-only run; never fail for it.

    Product: a finished review is always kept. If paths changed while Grok ran
    (dev servers, logs, other editors, etc.), append a short informational note
    so the operator knows findings may be slightly stale. Do not raise.

    Separate from Grok *reporting* edits in JSON (still unexpected-edits) and from
    code/verify worktree escape checks. Fingerprints are content/mode based and
    compare both directions so the note stays accurate when it fires.
    """
    if baseline is None:
        return
    try:
        after = worktree_escape.repo_change_fingerprint(pathlib.Path(run.repository))
    except GrokWrapperError as exc:
        msg = (
            "Could not check whether files changed during this review "
            "(informational only): {}".format(exc)
        )
        _log("_report_repo_fs_drift", msg)
        warnings.append(msg)
        progress.safe_emit("validate", msg, level="warning")
        return
    added_or_changed = {relative for relative, _fingerprint in (after - baseline)}
    removed_or_changed = {relative for relative, _fingerprint in (baseline - after)}
    new_changes = sorted(added_or_changed | removed_or_changed)
    if new_changes:
        # Cap the list so a huge concurrent build does not blow the envelope.
        shown = new_changes[:20]
        more = len(new_changes) - len(shown)
        detail = ", ".join(shown) + (" (+{} more)".format(more) if more > 0 else "")
        msg = (
            "Files changed during this review (informational only; findings still apply): "
            "{}".format(detail)
        )
        _log("_report_repo_fs_drift", "FS drift (informational): {}".format(new_changes))
        warnings.append(msg)
        progress.safe_emit(
            "validate",
            msg,
            level="warning",
            data={"changedFiles": new_changes, "failClosed": False},
        )
        return
    progress.safe_emit("validate", "no file changes detected during this review")


def _grok_reported_changes(parsed: Optional[dict]) -> List[str]:
    """Collect every file-change indicator Grok reported in its JSON output (empty when clean)."""
    if not isinstance(parsed, dict):
        return []
    changes: List[str] = []
    for key in _CHANGE_KEYS:
        value = parsed.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    changes.append(item)
        elif isinstance(value, dict) and value:
            changes.append(key)
        elif isinstance(value, str) and value.strip():
            changes.append(value)
    return changes


def _is_same_model_family(effective: str, requested_model: str) -> bool:
    """True iff ``effective`` is the requested model exactly, or a hyphen-delimited sub-variant of it.

    A raw ``startswith`` is wrong: requesting ``grok-4`` would then accept
    ``grok-4.5`` (Grok dogfood #4), silently treating a different model family as
    a success. The boundary is a literal ``-`` separator, so ``grok-4.5`` accepts
    the exact ``grok-4.5`` and build variants like ``grok-4.5-build``, while
    ``grok-4`` accepts ``grok-4`` / ``grok-4-...`` but NOT ``grok-4.5``.
    """
    return effective == requested_model or effective.startswith(requested_model + "-")


def _assert_effective_model(result: grokcli.GrokRunResult, requested_model: str) -> str:
    """Fail closed as model-unavailable unless the effective model is in the requested family."""
    effective = result.effective_model
    if not isinstance(effective, str) or not _is_same_model_family(effective, requested_model):
        _log(
            "_assert_effective_model",
            "effective model {!r} is not in the requested {!r} family".format(effective, requested_model),
        )
        raise GrokWrapperError(
            "model-unavailable",
            "grok ran model {!r} which is not in the requested {!r} family".format(
                effective, requested_model
            ),
            {"requestedModel": requested_model, "effectiveModel": effective},
        )
    return effective


def _execute_and_verify(
    run: ModeRun,
    home: PrivateHome,
    policy: SandboxPolicy,
    prompt_path: pathlib.Path,
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    result_holder: Optional[List[Optional[grokcli.GrokRunResult]]] = None,
    warnings: Optional[List[str]] = None,
) -> Tuple[grokcli.GrokRunResult, dict, str]:
    """Run Grok, enforce model family and sandbox write-confinement.

    F1-execute-and-verify-drops-result: ``grokcli.execute`` produces a REAL result
    before post-run checks (model family, sandbox). ``result_holder`` is populated
    immediately so a later raise still carries the redacted answer on the failure
    envelope.

    Review/read-only product rule: never fail solely because the tree moved or
    because Grok listed paths under change-shaped JSON keys. Those become
    informational ``warnings`` when provided. Write confinement for code/verify
    remains elsewhere (worktree escape checks).
    """
    leader_socket = runstate.allocate_leader_socket(home.home_dir, run_paths.run_id)
    spec = GrokRunSpec(
        binary=run.binary,
        cwd=run.cwd,
        model=run.requested_model,
        prompt_file=prompt_path,
        output_schema=run.output_schema,
        elicit_schema=run.elicit_schema,
        tools=run.tools,
        allow_rules=(),
        sandbox=policy,
        permission_mode=grokcli.HEADLESS_PERMISSION_MODE,
        max_turns=run.max_turns,
        timeout_seconds=run.timeout_seconds,
        leader_socket=leader_socket,
        session_id=str(uuid.uuid4()),
        subagents_enabled=False,
        web_access=run.web_access,
        home=home,
    )
    result = grokcli.execute(spec, progress)
    # Publish the produced result IMMEDIATELY so a post-run check raising below
    # still lets the caller carry the completed (redacted) answer on the failure
    # envelope (F1-execute-and-verify-drops-result).
    if result_holder is not None:
        result_holder[0] = result

    effective = _assert_effective_model(result, run.requested_model)
    progress.safe_emit("validate", "effective model is in the requested family", data={"effectiveModel": effective})

    sandbox_obj = verify_enforcement(home, policy)
    progress.safe_emit("sandbox", "sandbox write-confinement verified", data={"profile": sandbox_obj["reportedProfile"]})

    if run.detect_unexpected_edits:
        changes = _grok_reported_changes(result.parsed)
        if changes:
            shown = changes[:20]
            more = len(changes) - len(shown)
            detail = ", ".join(shown) + (" (+{} more)".format(more) if more > 0 else "")
            msg = (
                "Grok listed file-change fields during this read-only review "
                "(informational only; findings still apply): {}".format(detail)
            )
            _log(
                "_execute_and_verify",
                "grok reported {} file change(s) in a read-only run (informational)".format(len(changes)),
            )
            if warnings is not None:
                warnings.append(msg)
            progress.safe_emit(
                "validate",
                msg,
                level="warning",
                data={"changedFiles": changes, "failClosed": False},
            )
        else:
            progress.safe_emit("validate", "read-only run reported no file changes")

    return result, sandbox_obj, effective


def _clean_extra_temp_dirs(extra_temp_dirs: Tuple[pathlib.Path, ...], warnings: List[str]) -> None:
    """Best-effort removal of mode-owned temp working dirs (reason cwd). Never raises."""

    def _on_error(_func: object, failed_path: str, _exc_info: object) -> None:
        try:
            os.chmod(failed_path, stat.S_IWUSR | stat.S_IRUSR)
            os.remove(failed_path)
        except OSError as exc:
            warnings.append("could not remove temporary path {}".format(failed_path))
            _log("_clean_extra_temp_dirs", "failed to remove {}: {}".format(failed_path, exc))

    for directory in extra_temp_dirs:
        try:
            shutil.rmtree(str(directory), onerror=_on_error)
        except OSError as exc:
            warnings.append("could not remove temporary directory {}".format(directory))
            _log("_clean_extra_temp_dirs", "rmtree failed for {}: {}".format(directory, exc))


def run_grok_mode(run: ModeRun) -> dict:
    """Execute the shared Grok run lifecycle for ``run`` and return a validated C4 envelope.

    create_run -> run.json -> create_private_home -> (execute + verify) with a
    guaranteed single private-home teardown in the finally, plus best-effort
    cleanup of any mode-owned temp working dirs. Every classified failure
    (version-mismatch, model-unavailable, sandbox-failure, cli-failure,
    unexpected-edits, ...) is returned as a failure envelope; the private home
    is destroyed on every path. Any UNCLASSIFIED exception that escapes the
    lifecycle (a raw OSError, an envelope-build error) is terminalized under the
    REAL run id via ``terminalize_unexpected_failure`` so the entrypoint never
    synthesizes a dangling run id and run.json never stays at status="running".
    """
    run_paths: Optional[runstate.RunPaths] = None
    progress: Optional[ProgressWriter] = None
    # Round5 cleanup-outcome-lost-on-terminalize: the body creates its HomeCleanup
    # locally; this one-element holder carries it back so the outer terminalizer
    # can surface a FAILED private-home teardown even on the raw-exception / SIGTERM
    # path (where the exception, not a return value, leaves the body).
    home_cleanup_holder: List[Optional[HomeCleanup]] = [None]
    # F1-sigterm-drops-result: the same holder pattern for the completed Grok result, so
    # a SIGTERM escaping the body after Grok finished still carries the redacted answer.
    result_holder: List[Optional[grokcli.GrokRunResult]] = [None]
    try:
        # create_run is INSIDE the try (F1-create-run-outside-try) so a mid-create
        # failure terminalizes the REAL run rather than orphaning its on-disk dir
        # under a synthesized id.
        run_paths = runstate.create_run(run.mode)
        progress = ProgressWriter(run_paths.run_id, run_paths.progress_path)
        return _run_grok_mode_body(run, run_paths, progress, home_cleanup_holder, result_holder)
    except BaseException as exc:  # last-resort: terminalize under the REAL run id
        # BaseException (not just Exception) so a SIGTERM-driven SystemExit or a
        # KeyboardInterrupt mid-run still terminalizes the run, tears the private
        # home down (inner finally already ran), and emits exactly one C4 envelope
        # instead of exiting with run.json stuck at "running" and no stdout
        # envelope (F5-sigterm-bypasses-envelope).
        paths = run_paths if run_paths is not None else getattr(exc, "run_paths", None)
        if paths is None:
            # create_run failed before a run directory of ours even existed; there
            # is nothing on disk to terminalize. Let the entrypoint handle it.
            raise
        if progress is None:
            progress = ProgressWriter(paths.run_id, paths.progress_path)
        return terminalize_unexpected_failure(
            run_paths=paths,
            mode=run.mode,
            progress=progress,
            exc=exc,
            write_terminal_record=lambda: None,
            log=_log,
            cleanup=resolve_terminal_cleanup(home_cleanup_holder[0]),
            result=result_holder[0],
        )


def _run_grok_mode_body(
    run: ModeRun,
    run_paths: runstate.RunPaths,
    progress: ProgressWriter,
    home_cleanup_holder: List[Optional[HomeCleanup]],
    result_holder: List[Optional[grokcli.GrokRunResult]],
) -> dict:
    """The classified-failure lifecycle body of run_grok_mode (see its docstring)."""
    # Seed already has lifecycle=created; advance to running and merge mode fields.
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
                "requestedModel": run.requested_model,
                "repository": run.repository,
                "targetWorkspace": run.target_workspace,
                "status": "running",
            },
        )
    except Exception as exc:
        _log("_run_grok_mode_body", "could not advance lifecycle to running: {}".format(exc))
        runstate.write_run_record(run_paths, _run_record(run, run_paths, "running"))
    progress.safe_emit("start", "{} run created".format(run.mode), data={"mode": run.mode})

    # Reap a crashed prior run's stranded credential-bearing private home on live
    # START (Grok dogfood-2 #1/#7), not only during an occasional 24h preflight.
    # Best-effort and safely windowed so a concurrently-active run is never reaped.
    runstate.best_effort_reap_stale_temp_homes(runstate.LIVE_START_STALE_HOME_MAX_AGE_SECONDS)

    warnings: List[str] = []
    home_cleanup: Optional[HomeCleanup] = None
    # F1-execute-and-verify-drops-result / F1-sigterm-drops-result: the caller-owned holder
    # is populated the instant grokcli.execute produces a result, so a post-run check that
    # raises OR a SIGTERM escaping the body still carries the completed redacted answer.
    sandbox_obj: Optional[dict] = None
    effective_model: Optional[str] = None
    outcome_error: Optional[GrokWrapperError] = None
    review_fs_baseline: Optional["frozenset"] = None
    # PR968 codex #2: tracks whether the success-path repo write-check already ran, so
    # the failure-path re-run below fires ONLY when the run failed before reaching it.
    repo_write_checked = False

    try:
        # Informational FS baseline for read-only modes (review). Capture soft-fails
        # so a git glitch never blocks the actual review.
        review_fs_baseline = _capture_review_fs_baseline(run, warnings)

        # Wave 1 auto-preflight: version-keyed short cache; miss re-checks pin + auth.
        from groklib import preflight_cache

        preflight_cache.ensure_ready(run.binary)
        progress.safe_emit("validate", "grok version check verified (preflight cache)")

        # Pre-spawn SECURITY GUARANTEE (D-PORT / SEC1): fail closed on any
        # platform without a captured Grok sandbox probe report BEFORE a private
        # home is created or Grok is ever spawned. verify_enforcement re-checks
        # this after the run, but that is too late -- an unprobed platform must
        # never run Grok to completion with an unverified sandbox.
        platformsupport.require_probed_platform_for_live()

        private_tmp = pathlib.Path(tempfile.gettempdir()).resolve()
        policy = policy_for_mode(run.mode, worktree=None, private_tmp=private_tmp)
        sandbox_toml = render_sandbox_toml(policy, real_home=source_grok_dir().parent)
        home = create_private_home(
            source_grok_dir=source_grok_dir(),
            auth_file_names=AUTH_FILE_NAMES,
            config_toml=render_config_toml(mode=run.mode),
            sandbox_toml=sandbox_toml,
        )
        # Arm the destroy guard IMMEDIATELY after the home exists, before any
        # progress.emit that could raise (SEC4): a non-GrokWrapperError escaping
        # here would otherwise skip the finally and leave the auth copy on disk.
        home_cleanup = HomeCleanup(home)
        # Publish it to the outer terminalizer at once (Round5): a raw exception or
        # SIGTERM escaping after this point must still surface the teardown outcome.
        home_cleanup_holder[0] = home_cleanup

        try:
            progress.safe_emit("authhome", "private home created", data={"home": str(home.home_dir)})
            prompt_path = _write_prompt_file(run_paths, run.prompt_text)
            _, sandbox_obj, effective_model = _execute_and_verify(
                run,
                home,
                policy,
                prompt_path,
                run_paths,
                progress,
                result_holder,
                warnings,
            )
            # FS drift audit (warn-only): concurrent processes / editors may touch
            # the tree during review; never discard a completed review for that.
            _report_repo_fs_drift(run, review_fs_baseline, progress, warnings)
            repo_write_checked = True
        finally:
            destroy_result = home_cleanup.destroy_once()
            progress.safe_emit(
                "cleanup", "private home destroyed", data={"cleanupStatus": destroy_result["status"]}
            )
    except GrokWrapperError as exc:
        outcome_error = exc
        _log("run_grok_mode", "{} failed: {} ({})".format(run.mode, exc.error_class, exc))
    finally:
        _clean_extra_temp_dirs(run.extra_temp_dirs, warnings)

    # Same FS drift audit on failure paths (timeout/malformed/etc.) so concurrent
    # dirt is still visible in warnings, without overriding the real error class.
    if review_fs_baseline is not None and not repo_write_checked:
        _report_repo_fs_drift(run, review_fs_baseline, progress, warnings)

    # S4 / Grok dogfood-2 #2: a FAILED auth-material teardown is FAIL-CLOSED, never
    # a silent exit-0-as-if-clean. Retry the teardown once (the first failure may
    # be transient); if it STILL cannot be confirmed clean, surface it as a
    # warning AND, when the run would otherwise have succeeded, flip the outcome to
    # a classified cleanup-failure so a copy of the operator's auth material that
    # might remain on disk is reported as a non-success outcome.
    if home_cleanup is not None and home_cleanup.result.get("status") == "failed":
        home_cleanup.retry_if_failed()
    cleanup_field = home_cleanup.result if home_cleanup is not None else {"status": "not-applicable", "detail": None}
    if home_cleanup is not None and home_cleanup.result.get("status") == "failed":
        warnings.append(
            "private home teardown reported failed: {}".format(home_cleanup.result.get("detail"))
        )
        if outcome_error is None:
            outcome_error = GrokWrapperError(
                AUTH_TEARDOWN_FAILED_ERROR_CLASS,
                "the private home auth-material teardown could not be confirmed clean",
                {"reason": "auth-teardown-failed", "cleanupStatus": "failed"},
            )

    result = result_holder[0]
    if outcome_error is None and result is not None and sandbox_obj is not None and effective_model is not None:
        progress.safe_emit("done", "{} run completed".format(run.mode))
        envelope = _success_envelope(
            run, run_paths, result, sandbox_obj, effective_model, cleanup_field, warnings
        )
        return _publish_terminal_envelope(
            run_paths, run.mode, envelope, lifecycle="completed", progress=progress, warnings=warnings
        )

    error = outcome_error if outcome_error is not None else GrokWrapperError(
        "cli-failure", "{} run did not complete".format(run.mode), {"reason": "incomplete-run"}
    )
    progress.safe_emit("done", "{} failed: {}".format(run.mode, error.error_class), level="error")
    envelope = _failure_envelope(
        run, run_paths, error, sandbox_obj, effective_model, cleanup_field, warnings, result=result
    )
    return _publish_terminal_envelope(
        run_paths, run.mode, envelope, lifecycle="failed", progress=progress, warnings=warnings
    )
