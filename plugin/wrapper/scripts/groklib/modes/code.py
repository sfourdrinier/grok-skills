# wrapper/scripts/groklib/modes/code.py
#
# `code` mode (spec 5.3): write-capable authority mode. Grok runs inside an
# ISOLATED external git worktree (never the operator's real checkout), branched
# from a COMMITTED base whose sufficiency is proven up front -- the wrapper
# never stashes, commits, copies, or approximates uncommitted current-checkout
# changes, and never copies or symlinks `.env` into the worktree. The full
# root-to-target repo rules (C7) are prepended to the task, and the task's first
# required action is to create a `.grok-run-<run-id>` cwd sentinel proving Grok
# is operating in the correct isolated workspace. Editing + terminal tools are
# allowed only inside the sandbox's write confinement; --web is permitted.
#
# The pre-Grok setup (worktree create + verify, optional offline `pnpm install`)
# and the post-Grok gate (cwd sentinel, diff confinement, original-checkout
# scan, and the workspace's FULL build gate) are the two hooks handed to the
# shared worktree lifecycle (_worktree.run_worktree_mode), which owns the
# private-home isolation, execution, model/sandbox verification, single-teardown
# cleanup, and the retained-worktree cleanup semantics.

import argparse
import json
import os
import pathlib
import stat
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import rules
from groklib import worktree as worktree_mod
from groklib import worktree_escape
from groklib.implementation_contract import assert_target_matches, load_contract_file
from groklib.code_handoff_finalize import code_handoff_finalize
from groklib.projectconfig import ProjectConfig, build_gate_command, install_command, load_project_config
from groklib.modes import _shared
from groklib.modes._worktree import (
    FinalizeStage,
    WorktreePrep,
    WorktreeStage,
    run_worktree_mode,
)
from groklib.modes.review import _resolve_target as _resolve_repo_target

# Editing + reading + terminal tools (Task 0 inventory). Write confinement is
# enforced by the sandbox workspace profile plus the worktree cwd; the tool
# allowlist here is what grants Grok the ability to author the change at all.
_TOOLS: Tuple[str, ...] = (
    "read_file",
    "grep",
    "list_dir",
    "search_replace",
    "write",
    "run_terminal_command",
)

_SENTINEL_PREFIX = ".grok-run-"
# Test/operator override for the package-manager executable the build gate +
# install shell out to. When unset, the argv uses the detected package-manager
# token (pnpm/npm/yarn/bun) resolved on PATH.
_PM_BINARY_ENV_VAR = "GROK_PACKAGE_MANAGER_BINARY"
_PACKAGE_JSON = "package.json"
_NODE_MODULES = "node_modules"
_COMMAND_TIMEOUT_SECONDS = 3600


def _log(function: str, message: str) -> None:
    log_stderr("modes.code", function, message)


def _resolve_pm_binary() -> Optional[str]:
    """Resolve the package-manager binary override (GROK_PACKAGE_MANAGER_BINARY), else None.

    None means "use the detected package-manager token verbatim" (pnpm/npm/yarn/
    bun on PATH). A test or operator points the override at a specific executable.
    """
    override = os.environ.get(_PM_BINARY_ENV_VAR)
    if override is None or not override.strip():
        return None
    return os.path.expanduser(override)


def _apply_pm_binary(argv: List[str], pm_binary: Optional[str]) -> List[str]:
    """Swap argv[0] (the package-manager token) for the resolved override binary when set."""
    if pm_binary is None or not argv:
        return argv
    return [pm_binary] + list(argv[1:])


def _run_recorded_command(argv: List[str], cwd: pathlib.Path, purpose: str) -> dict:
    """Run one required wrapper-side command in ``cwd``, returning a C4 commands[] record.

    A spawn failure (binary absent, not executable) is a fail-closed
    ``validation-failure``: a required command that cannot even run has not
    passed. The nonzero-exit decision is left to the caller so the record is
    always captured on ``acc.commands`` before any failure is raised.

    Includes bounded redacted evidence (sha256 + 4k tails) per design §14.13.
    Always uses shell=False (argv list only).
    """
    from groklib.command_evidence import build_command_evidence

    argv_str = [str(token) for token in argv]
    start = time.monotonic()
    try:
        completed = subprocess.run(
            argv_str,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        from groklib.envelope import redact_secret_value_text

        safe_argv = [redact_secret_value_text(str(a)) for a in argv_str]
        _log(
            "_run_recorded_command",
            "could not run {} ({}): {}".format(purpose, safe_argv, exc),
        )
        raise GrokWrapperError(
            "validation-failure",
            "required command could not be run: {}".format(purpose),
            {"argv": safe_argv, "purpose": purpose},
        )
    duration = time.monotonic() - start
    return build_command_evidence(
        argv=argv_str,
        cwd=str(cwd),
        purpose=purpose,
        exit_status=int(completed.returncode),
        stdout=completed.stdout or b"",
        stderr=completed.stderr or b"",
        duration_seconds=round(duration, 6),
    )


def _target_in_worktree(worktree_path: pathlib.Path, target_relative: str) -> pathlib.Path:
    """Resolve the target workspace directory inside the worktree (worktree root when target is '')."""
    if target_relative:
        return worktree_path / target_relative
    return worktree_path


def _maybe_install_dependencies(
    stage: WorktreeStage,
    worktree_path: pathlib.Path,
    target_relative: str,
    package_manager: Optional[str],
    pm_binary: Optional[str],
) -> None:
    """Run the offline, lockfile-frozen install ONLY when the workspace has deps but no node_modules.

    No-op when no package manager was detected (a non-JS repo). Records the
    command on ``stage.acc.commands`` and fails closed (validation-failure) on a
    nonzero exit. The offline + lockfile-frozen flags guarantee the install never
    reaches the network and never mutates the lockfile.
    """
    if package_manager is None:
        return
    workspace_dir = _target_in_worktree(worktree_path, target_relative)
    has_manifest = (workspace_dir / _PACKAGE_JSON).is_file()
    has_node_modules = (workspace_dir / _NODE_MODULES).exists()
    if not has_manifest or has_node_modules:
        return
    argv = _apply_pm_binary(install_command(package_manager), pm_binary)
    record = _run_recorded_command(argv, worktree_path, "install")
    stage.acc.commands.append(record)
    if record["exitStatus"] != 0:
        raise GrokWrapperError(
            "validation-failure",
            "{} install failed inside the worktree (exit {})".format(package_manager, record["exitStatus"]),
            {"exitStatus": record["exitStatus"], "purpose": "install"},
        )


def _sentinel_directive(sentinel_name: str) -> str:
    """The positive-framed instruction making the cwd sentinel Grok's mandatory first action."""
    return (
        "Before doing anything else, your FIRST action MUST be to create an empty file named "
        "`{name}` in your current working directory (the isolated worktree root). Creating this "
        "sentinel confirms you are operating in the correct isolated workspace. After creating "
        "`{name}`, proceed with the task below.\n\n".format(name=sentinel_name)
    )


def _contract_directive(contract: Optional[dict]) -> str:
    """Render the operator contract as prompt text so Grok knows the objective,
    the acceptance criteria it must satisfy, and the write scopes it must stay
    inside. Enforcement stays wrapper-side (scopes/validation at finalize);
    this directive is steering, not the enforcement surface."""
    if not contract:
        return ""
    lines: List[str] = ["## Implementation contract (operator-supplied)", ""]
    objective = contract.get("objective") or ""
    if objective.strip():
        lines += ["Objective: {}".format(objective.strip()), ""]
    criteria = [c for c in contract.get("acceptanceCriteria") or [] if isinstance(c, str) and c.strip()]
    if criteria:
        lines.append("Acceptance criteria (ALL must hold when you finish):")
        lines += ["- {}".format(c.strip()) for c in criteria]
        lines.append("")
    scopes = contract.get("writeScopes") or []
    if scopes:
        lines.append("You may create or modify files ONLY within these paths (relative to the repo root):")
        lines += ["- {} ({})".format(s.get("path"), s.get("kind")) for s in scopes]
        lines.append("Changes outside these scopes will be rejected by the wrapper.")
        lines.append("")
    required = contract.get("requiredValidation") or []
    if required:
        lines.append("After implementing, these commands must exit 0 (the wrapper runs them):")
        lines += ["- {}".format(" ".join(e.get("argv", []))) for e in required]
        lines.append("")
    return "\n".join(lines) + "\n"


def _is_regular_file_no_symlink(path: pathlib.Path) -> bool:
    """True only when ``path`` is a REGULAR file and NOT a symlink (lstat, no following).

    Grok r3 #12 weak-cwd-sentinel: a bare ``.exists()`` follows symlinks and is
    satisfied by a symlink or a directory named like the sentinel, which Grok could
    plant WITHOUT actually operating in the worktree. Requiring a real regular file
    (lstat S_ISREG, so a symlink is rejected) makes the sentinel harder to spoof.
    """
    try:
        return stat.S_ISREG(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def _assert_cwd_sentinel(
    worktree: worktree_mod.ExternalWorktree, sentinel_name: str
) -> None:
    """Fail closed as wrong-working-directory unless the sentinel is a real file in the worktree and NOT the checkout.

    Grok's mandated first action creates ``sentinel_name`` in its cwd. A correct
    run leaves it inside the worktree; a run that somehow executed in the
    operator's real checkout leaves it there instead. Requiring a REGULAR-FILE
    sentinel (not a symlink or directory, Grok r3 #12) in the worktree AND absence
    of any entry by that name in the original checkout (``lexists`` catches a
    planted symlink there too) catches a missing sentinel (Grok never ran where we
    told it), a misplaced one (Grok ran in the wrong tree), and a spoofed one
    (symlink/dir standing in for the file) -- spec 5.3.3, hazards 13/15.
    """
    in_worktree = _is_regular_file_no_symlink(worktree.path / sentinel_name)
    in_checkout = os.path.lexists(str(worktree.repo_root / sentinel_name))
    if in_worktree and not in_checkout:
        return
    _log(
        "_assert_cwd_sentinel",
        "sentinel {} in_worktree={} in_checkout={}".format(sentinel_name, in_worktree, in_checkout),
    )
    raise GrokWrapperError(
        "wrong-working-directory",
        "the cwd sentinel proves Grok did not run in the isolated worktree",
        {
            "sentinel": sentinel_name,
            "inWorktree": in_worktree,
            "inOriginalCheckout": in_checkout,
            "worktree": str(worktree.path),
            "repository": str(worktree.repo_root),
        },
    )


def _read_workspace_manifest(workspace_dir: pathlib.Path) -> Tuple[str, Dict[str, object]]:
    """Read the workspace package.json name + scripts, failing closed when it is missing or malformed.

    The FULL build gate (spec 5.3.10) is mandatory, and it is unrunnable without
    a package.json that names the workspace, so a missing/unreadable/nameless
    manifest is a fail-closed validation-failure rather than a silently skipped
    gate.
    """
    manifest = workspace_dir / _PACKAGE_JSON
    if not manifest.is_file():
        raise GrokWrapperError(
            "validation-failure",
            "target workspace has no package.json; the required build gate cannot run",
            {"workspace": str(workspace_dir)},
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log("_read_workspace_manifest", "could not read {}: {}".format(manifest, exc))
        raise GrokWrapperError(
            "validation-failure",
            "could not read the workspace package.json",
            {"workspace": str(workspace_dir)},
        )
    if not isinstance(data, dict):
        raise GrokWrapperError(
            "validation-failure",
            "the workspace package.json is not a JSON object",
            {"workspace": str(workspace_dir)},
        )
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GrokWrapperError(
            "validation-failure",
            "the workspace package.json has no usable name for pnpm --filter",
            {"workspace": str(workspace_dir)},
        )
    scripts = data.get("scripts")
    return name, scripts if isinstance(scripts, dict) else {}


def _read_committed_manifest_fields(
    workspace_dir: pathlib.Path,
) -> Tuple[Optional[str], Optional[Dict[str, object]]]:
    """Best-effort read of the PRISTINE workspace name AND scripts at run START, before Grok edits.

    Returns ``(name, scripts)`` read from the committed base-commit manifest in
    the freshly-created (pre-Grok) worktree:

    * PR968 codex build-gate-filter: the post-Grok build gate must pin to the
      target's ORIGINAL identity, because Grok could rename the workspace
      package.json ``name`` to another existing workspace and redirect the gate
      onto a DIFFERENT package while the real target changes go unchecked. The
      committed ``name`` is captured up front so the gate never trusts the
      post-run name.
    * D1(b): the committed ``scripts`` are captured so the post-Grok gate can
      refuse to EXECUTE any gate script whose definition Grok added or changed
      during the run (a Grok-rewritten build/test/typecheck/lint script must
      never run in the operator environment).

    ``name`` is ``None`` when absent/blank; ``scripts`` distinguishes two cases
    the D1(b) comparison depends on: a returned ``{}`` means the base manifest
    was READABLE but declared no ``scripts`` (so a script the run adds is a
    genuine addition), whereas a returned ``None`` means the base manifest could
    NOT be read/parsed at all (so the gate fails closed and refuses to run any
    Grok-controllable script). This reader is deliberately non-raising; the
    mandatory manifest validation still runs fail-closed in the post-Grok build
    gate (``_read_workspace_manifest``), so this never masks a missing manifest.
    """
    manifest = workspace_dir / _PACKAGE_JSON
    if not manifest.is_file():
        return None, None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log("_read_committed_manifest_fields", "could not read committed manifest {}: {}".format(manifest, exc))
        return None, None
    if not isinstance(data, dict):
        return None, None
    name = data.get("name")
    resolved_name = name if isinstance(name, str) and name.strip() else None
    scripts = data.get("scripts")
    resolved_scripts: Dict[str, object] = scripts if isinstance(scripts, dict) else {}
    return resolved_name, resolved_scripts


def _build_gate_scripts(
    scripts: Dict[str, object],
    workspace_name: str,
    never_build_workspaces: Dict[str, Tuple[str, ...]],
) -> List[str]:
    """Resolve the build-gate script list for the workspace.

    NEVER-build workspaces (configured per-repo via ``.grok-skills.json``, empty
    by default) run the exact pinned validation list instead of building, even
    when a build script exists. Otherwise: ``build`` when a build script exists,
    else ``typecheck`` + ``lint`` (whichever of those the manifest actually
    defines); plus ``test`` whenever a test script exists. Only scripts the
    manifest actually declares are selected, so a plain repo with just a ``build``
    script gates on exactly that.
    """
    never_build = never_build_workspaces.get(workspace_name)
    if never_build is not None:
        return list(never_build)
    if "build" in scripts:
        gate = ["build"]
    else:
        gate = [name for name in ("typecheck", "lint") if name in scripts]
    if "test" in scripts:
        gate.append("test")
    return gate


def _gate_scripts_modified(
    pristine_scripts: Optional[Dict[str, object]],
    postrun_scripts: Dict[str, object],
    gate_script_names: List[str],
) -> List[str]:
    """The gate script names the wrapper WOULD execute that were ADDED or CHANGED during the run (D1(b)).

    Compares each gate script's post-run definition against its PRISTINE
    base-commit definition. A name is "modified" when the two definitions
    differ, which covers both an ADDED script (absent from the base, present
    now) and a CHANGED script (a different command string). Fail closed: when
    ``pristine_scripts`` is ``None`` the base manifest could not be read, so the
    run cannot prove Grok left the gate scripts untouched -- every gate script
    the wrapper would execute is treated as modified so the gate is refused
    rather than run against an unverifiable base.
    """
    if pristine_scripts is None:
        return list(gate_script_names)
    modified: List[str] = []
    for script in gate_script_names:
        if pristine_scripts.get(script) != postrun_scripts.get(script):
            modified.append(script)
    return modified


def _record_gate_scripts_modified_skip(stage: FinalizeStage, modified: List[str]) -> None:
    """Fail the run when Grok modified build-gate scripts (D1(b) hard fail).

    Previously this only warned while still returning success; that made the
    integrity gate optional theater. Now the finalize path raises so the envelope
    is status=failure with validation-failure.
    """
    from groklib import GrokWrapperError

    message = (
        "build gate refused (gate-scripts-modified): the run added or changed the "
        "gate script definition(s) {}; the wrapper will not execute Grok-modified "
        "build/test/typecheck/lint scripts and will not report success. Review the "
        "worktree and run the workspace build gate manually.".format(", ".join(modified))
    )
    _log("_run_build_gate", "build gate FAILED (gate-scripts-modified): {}".format(modified))
    stage.progress.safe_emit(
        "validate",
        "build gate refused: gate-scripts-modified",
        level="error",
        data={"modifiedGateScripts": modified},
    )
    raise GrokWrapperError(
        "validation-failure",
        message,
        {"modifiedGateScripts": list(modified), "reason": "gate-scripts-modified"},
    )


def _execute_build_gate(
    stage: FinalizeStage,
    workspace_dir: pathlib.Path,
    gate_script_names: List[str],
    package_manager: str,
    pm_binary: Optional[str],
    identity_name: str,
) -> None:
    """Run the resolved build-gate scripts IN the target workspace dir, recording each and requiring exit 0.

    The gate is pinned to the workspace by LOCATION: every command runs with cwd
    set to ``workspace_dir`` (the target directory inside the worktree), so a Grok
    rename of the package.json ``name`` can never redirect the gate onto a
    different package -- exactly the immutable-target guarantee, in a
    package-manager-agnostic way. Each command is appended to ``stage.acc.commands``
    BEFORE its exit status is checked, so a failing gate still surfaces the
    commands it ran. The first nonzero exit fails the run closed as
    validation-failure.
    """
    for script in gate_script_names:
        argv = _apply_pm_binary(build_gate_command(package_manager, script), pm_binary)
        record = _run_recorded_command(argv, workspace_dir, "build-gate:{}".format(script))
        stage.acc.commands.append(record)
        if record["exitStatus"] != 0:
            _log("_execute_build_gate", "build-gate script {!r} exited {}".format(script, record["exitStatus"]))
            raise GrokWrapperError(
                "validation-failure",
                "build gate command failed: {} {} (exit {})".format(
                    package_manager, script, record["exitStatus"]
                ),
                {
                    "script": script,
                    "workspace": identity_name,
                    "packageManager": package_manager,
                    "exitStatus": record["exitStatus"],
                },
            )
        stage.progress.safe_emit("validate", "build-gate command passed", data={"script": script})


def _record_no_gate_skip(stage: FinalizeStage, reason: str) -> None:
    """Record, as a warning, that no JS build gate ran for this target (non-JS repo, standalone)."""
    message = (
        "build gate skipped: {}. No package-manager build gate was run for this target; "
        "review the worktree diff and run any project-specific checks manually.".format(reason)
    )
    stage.acc.warnings.append(message)
    _log("_run_build_gate", "build gate SKIPPED (no-gate): {}".format(reason))
    stage.progress.safe_emit("validate", "build gate skipped: no package manager", data={"reason": reason})


def _run_build_gate(
    stage: FinalizeStage,
    target_relative: str,
    package_manager: Optional[str],
    pm_binary: Optional[str],
    never_build_workspaces: Dict[str, Tuple[str, ...]],
    original_workspace_name: Optional[str],
    pristine_scripts: Optional[Dict[str, object]],
) -> None:
    """Resolve and run the workspace FULL build gate, UNLESS Grok modified a gate script definition.

    Repo-agnostic (standalone grok-skills): when no package manager was detected
    for the repo, or the target has no package.json, there is no JS build gate to
    run -- the skip is recorded as an honest warning and the code result still
    returns (a plain non-JS repo is fully supported).

    The gate identity is pinned to ``original_workspace_name`` (the committed name
    captured BEFORE Grok ran) and every gate command runs with cwd set to the
    target workspace DIRECTORY, so a Grok rename of the manifest can never redirect
    the gate onto a different package. The manifest is still read post-run for its
    ``scripts`` (Grok may legitimately have added one), but never for the target
    IDENTITY.

    D1(b): before executing anything, the gate script definitions the wrapper
    WOULD run are compared against the pristine base-commit definitions
    (``pristine_scripts``). If any was ADDED or CHANGED by the run (or the base
    manifest could not be read), the gate is NOT executed -- it is skipped
    fail-closed and the skip is recorded in the envelope -- so a Grok-rewritten
    build/test/typecheck/lint script never runs in the operator environment. The
    code result (worktree changes) still returns; only the gate execution is
    refused. This complements the existing post-build-gate escape detection.
    """
    worktree = stage.worktree
    workspace_dir = _target_in_worktree(worktree.path, target_relative)

    if package_manager is None:
        _record_no_gate_skip(stage, "no package manager detected for this repository")
        return
    if not (workspace_dir / _PACKAGE_JSON).is_file():
        _record_no_gate_skip(stage, "the target workspace has no package.json")
        return

    postrun_name, scripts = _read_workspace_manifest(workspace_dir)
    identity_name = original_workspace_name if original_workspace_name is not None else postrun_name
    gate_script_names = _build_gate_scripts(scripts, identity_name, never_build_workspaces)

    modified = _gate_scripts_modified(pristine_scripts, scripts, gate_script_names)
    if modified:
        _record_gate_scripts_modified_skip(stage, modified)
        return

    _execute_build_gate(stage, workspace_dir, gate_script_names, package_manager, pm_binary, identity_name)


def run(args: argparse.Namespace) -> dict:
    """Resolve the target/base, then drive the shared worktree lifecycle for the write-capable code run."""
    binary = _shared.resolve_binary(args)
    task_text = _shared.resolve_task_text(args)
    repo_root, target_abs, target_relative = _resolve_repo_target(args.target)
    base = args.base
    project_config: ProjectConfig = load_project_config(repo_root)
    package_manager = project_config.package_manager
    pm_binary = _resolve_pm_binary()
    from groklib.web_defaults import resolve_web_access

    web_access = resolve_web_access("code", getattr(args, "web", None))

    # Optional operator-trusted implementation contract (design §14.3). Loaded
    # and validated BEFORE Grok so bad contracts never spawn a model.
    # Present empty/blank --contract-file is invalid (not "no contract").
    contract: Optional[dict] = None
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

    required_paths: Tuple[str, ...] = (target_relative,) if target_relative else ()

    # Entry baseline of the ORIGINAL checkout's tracked working-tree divergence,
    # captured at run start (before Grok runs). assert_changes_within uses it to
    # flag only NEWLY diverged tracked paths, so the operator's pre-existing
    # uncommitted tracked work is never misread as a run-introduced escape.
    original_baseline = worktree_escape.capture_original_checkout_baseline(repo_root)

    # PR968 codex build-gate-filter: capture the target's ORIGINAL committed
    # workspace name at run start so the post-Grok build gate pins to the intended
    # target's identity even if Grok renames the manifest during the run. A
    # one-element holder carries it from _prepare (pristine worktree) to _finalize.
    captured_workspace_name: List[Optional[str]] = [None]
    # D1(b): capture the target's PRISTINE (committed base-commit) gate script
    # definitions at run start, before Grok can edit them, so the post-Grok build
    # gate can refuse to execute any gate script Grok added or changed during the
    # run. None means the base manifest could not be read (fail-closed downstream).
    captured_workspace_scripts: List[Optional[Dict[str, object]]] = [None]

    def _prepare(stage: WorktreeStage) -> WorktreePrep:
        worktree_mod.assert_committed_base_sufficient(repo_root, base, required_paths)
        try:
            worktree = worktree_mod.create_external_worktree(
                repo_root=repo_root, base=base, run_id=stage.run_id
            )
        except BaseException as exc:
            # PR968 codex record-partial-worktree: when create_external_worktree's
            # rollback cannot remove a just-added worktree, it strands the worktree
            # + grok/code/<run> branch (marker-recorded) and annotates the error with
            # that run-bound identity. Enroll it into the run record (holder.worktree)
            # BEFORE re-raising so `cleanup --run-id` can rebuild and reap it;
            # otherwise run.json carries no worktree and cleanup would remove only the
            # run dir, orphaning the worktree + branch.
            stranded = worktree_mod.stranded_worktree_from_error(exc)
            if stranded is not None:
                stage.holder.worktree = stranded
            raise
        stage.holder.worktree = worktree
        worktree_mod.verify_external_worktree(worktree)

        # Read the PRISTINE (committed, pre-Grok) target name AND gate script
        # definitions before any Grok edit, in a single manifest read.
        captured_workspace_name[0], captured_workspace_scripts[0] = _read_committed_manifest_fields(
            _target_in_worktree(worktree.path, target_relative)
        )

        _maybe_install_dependencies(stage, worktree.path, target_relative, package_manager, pm_binary)

        instructions, rule_warnings = rules.discover_instruction_files_with_warnings(
            repo_root, target_abs, require_parity=project_config.require_rule_file_parity
        )
        stage.acc.warnings.extend(rule_warnings)
        instruction_entries = rules.instruction_envelope_entries(instructions)
        sentinel_name = _SENTINEL_PREFIX + stage.run_id
        task_with_sentinel = _sentinel_directive(sentinel_name) + _contract_directive(contract) + task_text
        prompt_text = rules.build_prompt_payload(instructions, task_with_sentinel)

        return WorktreePrep(
            worktree=worktree,
            cwd=worktree.path,
            prompt_text=prompt_text,
            instructions=instruction_entries,
            tools=_TOOLS,
        )

    def _finalize(stage: FinalizeStage) -> None:
        # Design §14.6 ordered post-Grok path (single function, no parallel pipeline).
        # Handoff JSON is written before any primary raise so phase-2 precedes the
        # runner's terminal envelope publish.
        sentinel_name = _SENTINEL_PREFIX + stage.run_id
        artifacts_dir = runstate.state_root() / "runs" / stage.run_id / "artifacts"

        def _build_gate() -> None:
            _run_build_gate(
                stage,
                target_relative,
                package_manager,
                pm_binary,
                project_config.never_build_workspaces,
                captured_workspace_name[0],
                captured_workspace_scripts[0],
            )

        code_handoff_finalize(
            stage=stage,
            sentinel_name=sentinel_name,
            contract=contract,
            artifacts_dir=artifacts_dir,
            original_baseline=original_baseline,
            run_build_gate=_build_gate,
            assert_changes_within=worktree_escape.assert_changes_within,
            assert_original_checkout_unmodified=worktree_escape.assert_original_checkout_unmodified,
            assert_cwd_sentinel=_assert_cwd_sentinel,
            run_recorded_command=_run_recorded_command,
        )

    return run_worktree_mode(
        mode="code",
        binary=binary,
        requested_model=args.model,
        web_access=web_access,
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        repository=str(repo_root),
        target_workspace=target_relative,
        worktree_retained=True,
        prepare=_prepare,
        finalize=_finalize,
    )
