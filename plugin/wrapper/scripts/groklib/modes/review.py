# wrapper/scripts/groklib/modes/review.py
#
# `review` mode (spec 5.1): full-context read-only code review. The working
# directory is a repository workspace resolved from --target; the complete
# applicable repo rules (C7, root-to-target) are prepended to the task; native
# tools are limited to read_file/grep/list_dir; the sandbox is read-only;
# subagents, memory, and (by default) web tools are off. A finished review is
# never discarded for tree drift or Grok listing change-shaped JSON keys - those
# are informational notes only. Hard fails stay for real setup/safety (auth,
# sandbox, runnable CLI check, model family). --schema is optional structured output.
# Shared lifecycle: private-home isolation, execution, sandbox verify, teardown.
#
# Opt-in external isolation (design §10 rev 9): only when --isolated is set.
# --base alone frames comparison in the prompt on the live checkout.

import argparse
import pathlib
from typing import Optional

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import rules
from groklib.projectconfig import load_project_config
from groklib.modes import _shared
from groklib.modes._envelope import terminalize_unexpected_failure
from groklib.progress import ProgressWriter
from groklib import review_isolation

_TOOLS = ("read_file", "grep", "list_dir")


def _log(function: str, message: str) -> None:
    log_stderr("modes.review", function, message)


def _resolve_target(target_relative: str) -> "tuple[pathlib.Path, pathlib.Path, str]":
    """Resolve (repo_root, target_abs, target_repo_relative), failing closed as invalid-target.

    Repo-agnostic (standalone grok-skills): the --target is resolved against the
    caller's cwd (or used verbatim when absolute), and the repo root is then
    derived FROM that resolved target via ``git rev-parse --show-toplevel`` -- NOT
    from the wrapper's install location. So a --target pointing at ANY repo on
    disk resolves to THAT repo's root and confines there. The target must resolve
    to an existing directory inside a git repository; a non-existent path, a
    non-directory, or a path not inside any git repo is a fail-closed
    invalid-target.
    """
    if not isinstance(target_relative, str) or not target_relative.strip():
        raise GrokWrapperError(
            "invalid-target", "review requires a non-empty --target workspace path", {"target": target_relative}
        )

    expanded = pathlib.Path(target_relative).expanduser()
    if expanded.is_absolute():
        target_abs = expanded.resolve()
    else:
        target_abs = (pathlib.Path.cwd() / expanded).resolve()

    if not target_abs.is_dir():
        _log("_resolve_target", "target {} is not an existing directory".format(target_abs))
        raise GrokWrapperError(
            "invalid-target",
            "the --target workspace does not exist as a directory: {}".format(target_relative),
            {"target": str(target_abs)},
        )

    # The repository root is the git toplevel that CONTAINS the resolved target.
    repo_root = _shared.repo_root_for_path(target_abs).resolve()

    try:
        target_repo_relative = target_abs.relative_to(repo_root).as_posix()
    except ValueError:
        # git toplevel of the target cannot fail to contain the target, but guard
        # fail-closed against a symlinked/normalized-path edge case anyway.
        _log("_resolve_target", "target {} escapes derived repo root {}".format(target_abs, repo_root))
        raise GrokWrapperError(
            "invalid-target",
            "the --target workspace resolves outside its own repository",
            {"repoRoot": str(repo_root), "target": str(target_abs)},
        )

    # "." resolves target_abs == repo_root, whose relative_to is "."; normalize
    # to "" so the recorded targetWorkspace names the repo root itself cleanly.
    if target_repo_relative == ".":
        target_repo_relative = ""
    return repo_root, target_abs, target_repo_relative


def _frame_task_with_base(task_text: str, base: Optional[str]) -> str:
    if not base or not str(base).strip():
        return task_text
    return (
        "Comparison base ref for this review (framing only; not an isolation trigger): "
        "{}\n\n{}".format(str(base).strip(), task_text)
    )


def run(args: argparse.Namespace) -> dict:
    """Prepare the C7 rules payload for --target and drive the shared read-only review run."""
    binary = _shared.resolve_binary(args)
    task_text = _shared.resolve_task_text(args)
    output_schema = _shared.load_output_schema(args)
    task_text = _frame_task_with_base(task_text, getattr(args, "base", None))

    repo_root, target_abs, target_repo_relative = _resolve_target(args.target)

    project_config = load_project_config(repo_root)
    instructions = rules.discover_instruction_files(
        repo_root, target_abs, require_parity=project_config.require_rule_file_parity
    )
    prompt_text = rules.build_prompt_payload(instructions, task_text)
    instruction_entries = rules.instruction_envelope_entries(instructions)

    from groklib.web_defaults import resolve_web_access

    cwd = target_abs
    pre_paths = None
    isolation = None
    want_isolated = bool(getattr(args, "isolated", False))

    try:
        if want_isolated:
            # Mint run id first so isolation worktree path is bound to the same id
            # that owns the durable envelope (design §10).
            pre_paths = runstate.create_run("review")
            isolation = review_isolation.prepare_review_isolation(
                repo_root=repo_root, run_id=pre_paths.run_id
            )
            # Persist worktree identity before Grok runs so cleanup can reap the
            # worktree if this process is killed before the finally block.
            try:
                record = runstate.load_run_record(pre_paths.run_id)
                rev = int(record.get("recordRevision", 0))
                runstate.cas_update_run_record(
                    pre_paths,
                    rev,
                    {
                        "repository": str(repo_root),
                        "targetWorkspace": target_repo_relative,
                        "worktreePath": str(isolation.worktree_path),
                        "worktreeBranch": isolation.branch,
                        "baseRevision": isolation.base_revision,
                        "status": "running",
                    },
                )
            except Exception as exc:  # best-effort; still fail closed if CAS is broken
                raise GrokWrapperError(
                    "isolation-unavailable",
                    "could not record isolation worktree on the run for cleanup: {}".format(exc),
                    {
                        "worktreePath": str(isolation.worktree_path),
                        "worktreeBranch": isolation.branch,
                    },
                ) from exc
            # Map target into the isolation worktree (same relative path).
            if target_repo_relative:
                cwd = isolation.worktree_path / target_repo_relative
            else:
                cwd = isolation.worktree_path
            if not cwd.is_dir():
                raise GrokWrapperError(
                    "isolation-unavailable",
                    "isolated worktree is missing the --target path {!r}".format(target_repo_relative),
                    {"worktreePath": str(isolation.worktree_path), "target": target_repo_relative},
                )

        mode_run = _shared.ModeRun(
            mode="review",
            binary=binary,
            requested_model=args.model,
            web_access=resolve_web_access("review", getattr(args, "web", None)),
            output_schema=output_schema,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
            prompt_text=prompt_text,
            cwd=cwd,
            tools=_TOOLS,
            instructions=instruction_entries,
            repository=str(repo_root),
            target_workspace=target_repo_relative,
            detect_unexpected_edits=True,
            tree_fingerprint_root=(
                isolation.worktree_path if isolation is not None else None
            ),
        )
        return _shared.run_grok_mode(mode_run, run_paths=pre_paths)
    except BaseException as exc:
        # Isolation setup (or target mapping) failed after create_run: terminalize
        # under the REAL run id — never fall back to live checkout, never leave
        # run.json stuck at "running" with a synthesized entrypoint id.
        if pre_paths is None:
            raise
        progress = ProgressWriter(pre_paths.run_id, pre_paths.progress_path)
        return terminalize_unexpected_failure(
            run_paths=pre_paths,
            mode="review",
            progress=progress,
            exc=exc,
            write_terminal_record=lambda: None,
            log=_log,
        )
    finally:
        if isolation is not None:
            review_isolation.cleanup_review_isolation(isolation)
