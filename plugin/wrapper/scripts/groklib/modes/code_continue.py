# wrapper/scripts/groklib/modes/code_continue.py
#
# Continuation helpers for `code --continue-run <runId>` (Task 2.2). Keeps
# modes/code.py under the 900-line cap: prior-run loading, worktree rebuild,
# contract persistence, and committed-manifest ref reads.

import json
import os
import pathlib
from typing import Dict, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import session_store
from groklib import worktree as worktree_mod
from groklib.implementation_contract import load_contract_file

_PACKAGE_JSON = "package.json"
_CONTRACT_JSON = "contract.json"
_TERMINAL_LIFECYCLES = frozenset({"completed", "failed", "canceled"})
_NO_SESSION_ARCHIVE_WARNING = (
    "prior run has no session archive; continuing in the same worktree with a fresh Grok session"
)


def _log(function: str, message: str) -> None:
    log_stderr("modes.code_continue", function, message)


def continuation_directive(prior_run_id: str, prior_iteration: int) -> str:
    """Prompt preamble naming the new iteration number and the prior run id."""
    return (
        "This is iteration {} continuing run {}. You are in the SAME isolated "
        "worktree as before; your prior changes are present. Apply the follow-up "
        "instructions below to the existing work. Do not revert prior progress "
        "unless the instructions say so.\n\n".format(prior_iteration + 1, prior_run_id)
    )


def write_contract_json(run_dir: pathlib.Path, contract: dict) -> None:
    """Persist the normalized contract next to other run artifacts (mode 0600)."""
    path = pathlib.Path(run_dir) / _CONTRACT_JSON
    text = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def load_persisted_contract(run_dir: pathlib.Path) -> Optional[dict]:
    """Load runs/<id>/contract.json when present; None when absent.

    Re-validates via load_contract_file so a tampered on-disk contract cannot
    bypass writeScopes / requiredValidation shape checks.
    """
    path = pathlib.Path(run_dir) / _CONTRACT_JSON
    if not path.is_file():
        return None
    return load_contract_file(path)


def read_committed_manifest_fields_from_ref(
    worktree_path: pathlib.Path,
    base_revision: str,
    target_relative: str,
) -> Tuple[Optional[str], Optional[Dict[str, object]]]:
    """Read package.json name+scripts from ``base_revision`` via ``git show`` (empty hooks).

    Continuation cannot trust the (already-edited) worktree file; this matches
    the pristine capture of a fresh run. Missing/unparseable -> (None, None).
    """
    if target_relative:
        rel = "{}/{}".format(target_relative.rstrip("/"), _PACKAGE_JSON)
    else:
        rel = _PACKAGE_JSON
    completed = worktree_mod._run_git(
        worktree_path, ["show", "{}:{}".format(base_revision, rel)]
    )
    if completed.returncode != 0:
        _log(
            "read_committed_manifest_fields_from_ref",
            "git show {} failed: {}".format(rel, (completed.stderr or "").strip()),
        )
        return None, None
    try:
        data = json.loads(completed.stdout or "")
    except (ValueError, TypeError) as exc:
        _log(
            "read_committed_manifest_fields_from_ref",
            "could not parse committed manifest {}: {}".format(rel, exc),
        )
        return None, None
    if not isinstance(data, dict):
        return None, None
    name = data.get("name")
    resolved_name = name if isinstance(name, str) and name.strip() else None
    scripts = data.get("scripts")
    resolved_scripts: Optional[Dict[str, object]]
    if isinstance(scripts, dict):
        resolved_scripts = scripts
    else:
        resolved_scripts = {}
    return resolved_name, resolved_scripts


def prior_iteration_from_record(record: dict) -> int:
    """Initial runs omit iteration (treated as 1); continuations store prior+1."""
    raw = record.get("iteration")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return 1


def resolve_continuation(
    prior_run_id: str,
) -> Tuple[dict, worktree_mod.ExternalWorktree, pathlib.Path, Optional[dict], list]:
    """Load and validate a prior code run for continuation.

    Returns (record, worktree, prior_run_dir, session_meta_or_None, warnings).
    Raises GrokWrapperError(invalid-target|usage-error) with the prior run id
    and what is missing.
    """
    if not isinstance(prior_run_id, str) or not prior_run_id.strip():
        raise GrokWrapperError(
            "usage-error",
            "code --continue-run requires a non-empty run id",
            {"continueRun": prior_run_id},
        )
    prior_id = prior_run_id.strip()
    try:
        record = runstate.load_run_record(prior_id)
    except runstate.UnknownRunError as exc:
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: no run record found".format(prior_id),
            {"runId": prior_id, "reason": "unknown-run"},
        ) from exc

    if record.get("mode") != "code":
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: prior mode is {!r}, expected 'code'".format(
                prior_id, record.get("mode")
            ),
            {"runId": prior_id, "mode": record.get("mode")},
        )

    lifecycle = record.get("lifecycle")
    if lifecycle not in _TERMINAL_LIFECYCLES:
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: lifecycle is {!r} (need a terminal run)".format(
                prior_id, lifecycle
            ),
            {"runId": prior_id, "lifecycle": lifecycle},
        )

    worktree = worktree_mod.rebuild_worktree_from_record(record)
    if worktree is None:
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: run record is missing worktree fields".format(prior_id),
            {
                "runId": prior_id,
                "reason": "missing-worktree-fields",
                "worktreePath": record.get("worktreePath"),
            },
        )
    if not worktree.path.is_dir():
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: worktree path is missing on disk ({})".format(
                prior_id, worktree.path
            ),
            {
                "runId": prior_id,
                "reason": "missing-worktree",
                "worktreePath": str(worktree.path),
            },
        )

    try:
        worktree_mod.verify_external_worktree(worktree)
    except GrokWrapperError as exc:
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: worktree failed verification ({})".format(
                prior_id, exc
            ),
            {
                "runId": prior_id,
                "reason": "worktree-verify-failed",
                "worktreePath": str(worktree.path),
                "detail": exc.detail or {},
            },
        ) from exc

    prior_run_dir = runstate.state_root() / "runs" / prior_id
    warnings: list = []
    session_meta = session_store.load_session_meta(prior_run_dir)
    if not (
        isinstance(session_meta, dict)
        and isinstance(session_meta.get("grokSessionId"), str)
        and session_meta.get("grokSessionId")
    ):
        session_meta = None
        warnings.append(_NO_SESSION_ARCHIVE_WARNING)

    return record, worktree, prior_run_dir, session_meta, warnings
