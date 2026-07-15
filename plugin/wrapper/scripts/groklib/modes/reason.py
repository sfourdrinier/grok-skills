# wrapper/scripts/groklib/modes/reason.py
#
# `reason` mode (spec 5.2): isolated artifact reasoning. The working directory
# is a fresh private temp dir OUTSIDE the repository; there is NO repo-wide rule
# discovery. Only the operator's explicitly-named --input artifacts are copied
# (read-only, 0o400) into that temp cwd and named in the prompt, and only the
# operator's explicitly-selected --rules-file contents are prepended, using the
# same C7 block format with their real repo-relative paths. Tools are absent
# unless at least one --input artifact is supplied, in which case read_file is
# enabled. The shared run lifecycle (_shared.run_grok_mode) owns private-home
# isolation, execution, sandbox verification, single-teardown cleanup, and the
# best-effort removal of the temp cwd.

import argparse
import hashlib
import os
import pathlib
import shutil
from typing import List, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import rules
from groklib.modes import _shared

_INPUT_FILE_MODE = 0o400
_INPUTS_BANNER = "=== SUPPLIED INPUT FILES (read-only, present in the working directory) ==="


def _log(function: str, message: str) -> None:
    log_stderr("modes.reason", function, message)


def _best_effort_remove_reason_cwd(cwd: pathlib.Path) -> None:
    """Remove the reason temp cwd, LOGGING each residual path (never silently swallowing).

    Grok dogfood #12: the temp cwd holds copies of the operator's --input
    artifacts, so a failed removal must be diagnosable, not hidden behind
    ``ignore_errors=True``. Runs on failure/escape paths, so it must never raise:
    per-path failures are logged via ``onerror`` and a top-level rmtree error is
    logged too.
    """

    def _on_error(_func: object, failed_path: str, _exc_info: object) -> None:
        _log(
            "_best_effort_remove_reason_cwd",
            "could not remove reason cwd path {} (may retain a copied --input artifact)".format(failed_path),
        )

    try:
        shutil.rmtree(str(cwd), onerror=_on_error)
    except OSError as exc:
        _log("_best_effort_remove_reason_cwd", "rmtree failed for reason cwd {}: {}".format(cwd, exc))


def _copy_input_files(cwd: pathlib.Path, source_paths: List[str]) -> List[str]:
    """Copy each --input artifact into ``cwd`` as a read-only (0o400) file, returning their names.

    Each source must be an existing regular file with a unique basename;
    anything else is a fail-closed invalid-target. The destination is created
    exclusively at mode 0o400 and then explicitly chmod'd to 0o400 as
    umask-independent belt-and-braces, so a supplied artifact can never be
    silently made writable inside the reasoning workspace.
    """
    names: List[str] = []
    seen = set()
    for raw in source_paths:
        source = pathlib.Path(raw)
        if not source.is_file():
            raise GrokWrapperError(
                "invalid-target", "input artifact does not exist: {}".format(source), {"input": str(source)}
            )
        name = source.name
        if name in seen:
            raise GrokWrapperError(
                "invalid-target",
                "two --input artifacts share the basename {!r}".format(name),
                {"input": str(source), "name": name},
            )
        seen.add(name)

        try:
            data = source.read_bytes()
        except OSError as exc:
            _log("_copy_input_files", "could not read input {}: {}".format(source, exc))
            raise GrokWrapperError(
                "invalid-target", "could not read the input artifact: {}".format(source), {"input": str(source)}
            )

        dest = cwd / name
        file_descriptor = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _INPUT_FILE_MODE)
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(str(dest), _INPUT_FILE_MODE)
        names.append(name)

    return names


def _build_rule_instructions(rules_files: List[str]) -> Tuple[List[rules.InstructionFile], "str|None"]:
    """Build C7 InstructionFile blocks from explicitly-selected --rules-file paths (no discovery).

    Each rules file's real repo-relative path (relative to the repo root derived
    from the caller's cwd, standalone grok-skills) labels its block. A
    missing/unreadable rules file, or one outside the cwd's repository, is a
    fail-closed invalid-target. Returns the instruction list and the repository
    root string (None when no rules files were selected).
    """
    if not rules_files:
        return [], None

    repo_root = _shared.repo_root_for_path(pathlib.Path.cwd()).resolve()
    instructions: List[rules.InstructionFile] = []
    for raw in rules_files:
        source = pathlib.Path(raw).resolve()
        if not source.is_file():
            raise GrokWrapperError(
                "invalid-target", "rules file does not exist: {}".format(source), {"rulesFile": str(source)}
            )
        try:
            repo_relative = source.relative_to(repo_root).as_posix()
        except ValueError:
            _log("_build_rule_instructions", "rules file {} is outside repo root {}".format(source, repo_root))
            raise GrokWrapperError(
                "invalid-target",
                "the --rules-file resolves outside the repository",
                {"repoRoot": str(repo_root), "rulesFile": str(source)},
            )
        try:
            content_bytes = source.read_bytes()
        except OSError as exc:
            _log("_build_rule_instructions", "could not read rules file {}: {}".format(source, exc))
            raise GrokWrapperError(
                "invalid-target", "could not read the rules file: {}".format(source), {"rulesFile": str(source)}
            )
        try:
            content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise GrokWrapperError(
                "invalid-target", "the --rules-file is not valid UTF-8: {}".format(source), {"rulesFile": str(source)}
            )
        instructions.append(
            rules.InstructionFile(
                path=source,
                repo_relative=repo_relative,
                content_bytes=content_bytes,
                sha256=hashlib.sha256(content_bytes).hexdigest(),
            )
        )

    return instructions, str(repo_root)


def _build_prompt(instructions: List[rules.InstructionFile], input_names: List[str], task_text: str) -> str:
    """Assemble the reason prompt: input naming, then task, prefixed by C7 rule blocks when present.

    Without any --rules-file the prompt carries NO repository-rules block at all
    (reason performs no rule discovery); with rules files the exact C7 template
    from rules.build_prompt_payload wraps the same body.
    """
    body_parts: List[str] = []
    if input_names:
        body_parts.append(_INPUTS_BANNER)
        for name in input_names:
            body_parts.append("- {}".format(name))
        body_parts.append("")
    body_parts.append(task_text)
    body = "\n".join(body_parts)

    if instructions:
        return rules.build_prompt_payload(instructions, body)
    return body


def run(args: argparse.Namespace) -> dict:
    """Prepare the isolated reasoning workspace and drive the shared read-only reason run."""
    binary = _shared.resolve_binary(args)
    task_text = _shared.resolve_task_text(args)
    output_schema = _shared.load_output_schema(args)

    input_paths = list(getattr(args, "input", []) or [])
    rules_files = list(getattr(args, "rules_file", []) or [])

    rule_instructions, repository = _build_rule_instructions(rules_files)

    cwd = _shared.make_reason_cwd()
    try:
        input_names = _copy_input_files(cwd, input_paths)
    except BaseException:
        # The temp cwd exists but the shared runner (which owns its cleanup via
        # extra_temp_dirs) is not reached on this path, so remove it here before
        # the classified failure propagates to the entrypoint.
        _log("run", "removing reason cwd after input-copy failure")
        _best_effort_remove_reason_cwd(cwd)
        raise

    prompt_text = _build_prompt(rule_instructions, input_names, task_text)
    tools: Tuple[str, ...] = ("read_file",) if input_names else ()

    from groklib.web_defaults import resolve_web_access

    mode_run = _shared.ModeRun(
        mode="reason",
        binary=binary,
        requested_model=args.model,
        web_access=resolve_web_access("reason", getattr(args, "web", None)),
        output_schema=output_schema,
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        prompt_text=prompt_text,
        cwd=cwd,
        tools=tools,
        instructions=rules.instruction_envelope_entries(rule_instructions),
        repository=repository,
        target_workspace=None,
        detect_unexpected_edits=False,
        extra_temp_dirs=(cwd,),
    )
    try:
        return _shared.run_grok_mode(mode_run)
    except BaseException:
        # F4: run_grok_mode owns the temp cwd cleanup (via extra_temp_dirs) ONCE
        # it reaches its own finally, but a create_run collision raises BEFORE
        # that finally is armed -- leaking the reason cwd, which holds the copied
        # --input artifacts. Remove it here on any escape (a no-op if the runner
        # already cleaned it) before the failure propagates to the entrypoint.
        _log("run", "removing reason cwd after run_grok_mode escaped before its own cleanup")
        _best_effort_remove_reason_cwd(cwd)
        raise
