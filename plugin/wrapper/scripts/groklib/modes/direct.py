# wrapper/scripts/groklib/modes/direct.py
#
# `code --integration direct`: Grok edits the operator's real repo working tree
# under private auth home + sandbox write-confined to the repo root + post-run
# deny/scope/dirty guards. No external worktree. Product companion default after
# per-repo consent; bare wrapper still defaults to --integration worktree when
# the flag is omitted (fail-closed). See modes/_direct.py for the security
# honesty statement (no isolation, no full rollback; deny scan is post-run
# best-effort).

import argparse
import pathlib
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import rules
from groklib import worktree_escape
from groklib.implementation_contract import assert_target_matches, load_contract_file
from groklib.modes import code as code_mode
from groklib.modes import code_continue
from groklib.modes import _shared
from groklib.modes._direct import DirectPrep, DirectStage, run_direct_mode
from groklib.modes.direct_finalize import (
    capture_git_dir_guard,
    capture_pristine_manifest,
    finalize_direct,
)
from groklib.modes.review import _resolve_target as _resolve_repo_target
from groklib.projectconfig import ProjectConfig, load_project_config

_TOOLS: Tuple[str, ...] = code_mode._TOOLS


def _log(function: str, message: str) -> None:
    log_stderr("modes.direct", function, message)


def run(args: argparse.Namespace) -> dict:
    """Resolve target/contract, build the prompt, drive the hardened-direct lifecycle."""
    binary = _shared.resolve_binary(args)
    task_text = _shared.resolve_task_text(args)
    from groklib.web_defaults import resolve_web_access

    # Web defaults match code (opt-in); mode label "direct" is unknown to the
    # table and falls through to False when neither --web nor --no-web is set.
    web_access = resolve_web_access("code", getattr(args, "web", None))
    pm_binary = code_mode._resolve_pm_binary()
    force = bool(getattr(args, "force", False))

    repo_root, target_abs, target_relative = _resolve_repo_target(args.target)
    # Optional operator-trusted implementation contract (writeScopes + requiredValidation).
    contract = None
    contract_file = getattr(args, "contract_file", None)
    if contract_file is not None:
        if not str(contract_file).strip():
            raise GrokWrapperError(
                "implementation-contract-invalid",
                "--contract-file was provided but is empty; omit the flag or pass a path",
                {"contractFile": contract_file},
            )
        contract = load_contract_file(pathlib.Path(str(contract_file).strip()))
        cli_target = target_relative if target_relative else "."
        assert_target_matches(contract, cli_target)

    project_config: ProjectConfig = load_project_config(repo_root)
    package_manager = project_config.package_manager

    # D1(b) pristine manifest from HEAD before Grok can edit package.json scripts.
    captured_workspace_name: List[Optional[str]] = [None]
    captured_workspace_scripts: List[Optional[Dict[str, object]]] = [None]
    captured_workspace_name[0], captured_workspace_scripts[0] = capture_pristine_manifest(
        repo_root, target_relative
    )

    def _prepare(stage: DirectStage) -> DirectPrep:
        baseline_fp = worktree_escape.repo_change_fingerprint(repo_root)
        dirty_paths = frozenset(relative for relative, _fp in baseline_fp)
        baseline_git_fp = capture_git_dir_guard(repo_root)

        if contract is not None:
            code_continue.write_contract_json(stage.run_paths.run_dir, contract)

        instructions, rule_warnings = rules.discover_instruction_files_with_warnings(
            repo_root, target_abs, require_parity=project_config.require_rule_file_parity
        )
        stage.acc.warnings.extend(rule_warnings)
        instruction_entries = rules.instruction_envelope_entries(instructions)
        prompt_parts = [code_mode._contract_directive(contract), task_text]
        prompt_text = rules.build_prompt_payload(instructions, "".join(prompt_parts))

        return DirectPrep(
            cwd=repo_root,
            prompt_text=prompt_text,
            instructions=instruction_entries,
            tools=_TOOLS,
            baseline_fp=baseline_fp,
            dirty_paths=dirty_paths,
            baseline_git_fp=baseline_git_fp,
        )

    def _finalize(stage) -> None:
        finalize_direct(
            stage,
            contract=contract,
            target_relative=target_relative,
            package_manager=package_manager,
            pm_binary=pm_binary,
            never_build_workspaces=project_config.never_build_workspaces,
            original_workspace_name=captured_workspace_name[0],
            pristine_scripts=captured_workspace_scripts[0],
        )

    return run_direct_mode(
        binary=binary,
        requested_model=args.model,
        web_access=web_access,
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        repository=str(repo_root),
        target_workspace=target_relative,
        force=force,
        prepare=_prepare,
        finalize=_finalize,
    )
