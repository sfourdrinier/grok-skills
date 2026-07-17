# wrapper/scripts/groklib/modes/code_continue.py
#
# Continuation helpers for `code --continue-run <runId>` (Task 2.2). Keeps
# modes/code.py under the 900-line cap: prior-run loading, worktree rebuild,
# contract persistence, single-lineage claim, and committed-manifest ref reads.

import json
import os
import pathlib
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr, runstate
from groklib import session_store
from groklib import worktree as worktree_mod
from groklib.code_handoff_finalize import _contract_sha256
from groklib.implementation_contract import assert_target_matches, load_contract_file

_PACKAGE_JSON = "package.json"
_CONTRACT_JSON = "contract.json"
_HANDOFF_JSON = "implementation-handoff.json"
_TERMINAL_LIFECYCLES = frozenset({"completed", "failed", "canceled"})
_NO_SESSION_ARCHIVE_WARNING = (
    "prior run has no session archive; continuing in the same worktree with a fresh Grok session"
)
# Max iteration number on a successful continue (initial run counts as 1).
MAX_CONTINUATION_ITERATION = 20


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


def _prior_contract_sha256(prior_run_dir: pathlib.Path) -> Optional[str]:
    """Return non-null contractSha256 from the prior handoff manifest, if any."""
    path = pathlib.Path(prior_run_dir) / _HANDOFF_JSON
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        _log("_prior_contract_sha256", "could not read handoff {}: {}".format(path, exc))
        return None
    if not isinstance(doc, dict):
        return None
    sha = doc.get("contractSha256")
    if isinstance(sha, str) and sha.strip():
        return sha.strip()
    return None


def _assert_base_is_commit(worktree: worktree_mod.ExternalWorktree, prior_id: str) -> None:
    """Fail closed when baseRevision is not a commit object in the worktree."""
    base = worktree.base_revision
    completed = worktree_mod._run_git(
        worktree.path, ["cat-file", "-e", "{}^{{commit}}".format(base)]
    )
    if completed.returncode != 0:
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: baseRevision {!r} is not a commit in the worktree".format(
                prior_id, base
            ),
            {
                "runId": prior_id,
                "reason": "invalid-base-revision",
                "baseRevision": base,
                "worktreePath": str(worktree.path),
            },
        )


def _assert_no_concurrent_writer(worktree_path: pathlib.Path, prior_id: str) -> None:
    """Fail closed when another non-terminal run records the same worktreePath."""
    want = str(worktree_path)
    for other_id in runstate.list_run_ids():
        if other_id == prior_id:
            continue
        try:
            other = runstate.load_run_record(other_id)
        except runstate.UnknownRunError:
            continue
        if other.get("worktreePath") != want:
            continue
        lifecycle = other.get("lifecycle")
        if lifecycle in _TERMINAL_LIFECYCLES:
            continue
        raise GrokWrapperError(
            "invalid-target",
            "cannot continue run {}: another non-terminal run {} holds the same worktree".format(
                prior_id, other_id
            ),
            {
                "runId": prior_id,
                "reason": "concurrent-writer",
                "conflictingRunId": other_id,
                "worktreePath": want,
                "conflictingLifecycle": lifecycle,
            },
        )


def _assert_not_already_continued(record: dict, prior_id: str) -> None:
    child = record.get("continuedByRunId")
    if isinstance(child, str) and child.strip():
        raise GrokWrapperError(
            "invalid-target",
            "run {} was already continued by {}; continue THAT run (or clean up and start fresh)".format(
                prior_id, child.strip()
            ),
            {
                "runId": prior_id,
                "reason": "already-continued",
                "continuedByRunId": child.strip(),
            },
        )


def _assert_iteration_cap(prior_iteration: int, prior_id: str) -> None:
    if prior_iteration >= MAX_CONTINUATION_ITERATION:
        raise GrokWrapperError(
            "usage-error",
            "cannot continue run {}: continuation would exceed the maximum iteration "
            "cap of {}".format(prior_id, MAX_CONTINUATION_ITERATION),
            {
                "runId": prior_id,
                "priorIteration": prior_iteration,
                "maxContinuationIteration": MAX_CONTINUATION_ITERATION,
            },
        )


def _load_and_pin_contract(
    prior_run_dir: pathlib.Path,
    record: dict,
    worktree: worktree_mod.ExternalWorktree,
    prior_id: str,
) -> Tuple[Optional[dict], List[str]]:
    """Load prior contract with integrity pin; return (contract, warnings)."""
    warnings: List[str] = []
    expected_sha = _prior_contract_sha256(prior_run_dir)
    contract: Optional[dict] = None
    try:
        contract = load_persisted_contract(prior_run_dir)
    except GrokWrapperError as exc:
        if expected_sha is not None:
            raise GrokWrapperError(
                "implementation-contract-invalid",
                "prior run had a contract but its persisted copy is missing",
                {
                    "runId": prior_id,
                    "reason": "persisted-contract-missing",
                    "contractSha256": expected_sha,
                    "detail": exc.detail or {},
                },
            ) from exc
        raise

    if expected_sha is not None and contract is None:
        raise GrokWrapperError(
            "implementation-contract-invalid",
            "prior run had a contract but its persisted copy is missing",
            {
                "runId": prior_id,
                "reason": "persisted-contract-missing",
                "contractSha256": expected_sha,
            },
        )

    if contract is not None:
        target_workspace = record.get("targetWorkspace")
        if not isinstance(target_workspace, str) or not target_workspace.strip():
            cli_target = "."
        else:
            cli_target = target_workspace
        assert_target_matches(contract, cli_target)

        if expected_sha is not None:
            actual = _contract_sha256(contract)
            if actual != expected_sha:
                raise GrokWrapperError(
                    "implementation-contract-invalid",
                    "persisted contract does not match the prior run's contractSha256 "
                    "(tampered or replaced)",
                    {
                        "runId": prior_id,
                        "reason": "contract-sha-mismatch",
                        "expectedSha256": expected_sha,
                        "actualSha256": actual,
                    },
                )

        for scope in contract.get("writeScopes") or []:
            if not isinstance(scope, dict):
                continue
            rel = scope.get("path")
            if not isinstance(rel, str) or not rel.strip():
                continue
            scope_path = worktree.path / rel
            if not scope_path.exists():
                warnings.append(
                    "writeScope path no longer exists in the worktree: {}".format(rel)
                )

    return contract, warnings


def claim_continuation(prior_run_id: str, child_run_id: str) -> dict:
    """CAS-stamp continuedByRunId on the prior run (single-lineage claim).

    Called once the new run id is known (prepare). Re-checks already-continued
    under lock so concurrent continues cannot fork siblings.
    """
    if not isinstance(child_run_id, str) or not child_run_id.strip():
        raise GrokWrapperError(
            "usage-error",
            "claim_continuation requires a non-empty child run id",
            {"continueRun": prior_run_id, "childRunId": child_run_id},
        )
    return runstate.cas_claim_continuation(prior_run_id.strip(), child_run_id.strip())


def resolve_continuation(
    prior_run_id: str,
) -> Tuple[dict, worktree_mod.ExternalWorktree, pathlib.Path, Optional[dict], list, Optional[dict]]:
    """Load and validate a prior code run for continuation.

    Returns (record, worktree, prior_run_dir, session_meta_or_None, warnings, contract).
    Raises GrokWrapperError(invalid-target|usage-error|implementation-contract-invalid)
    with the prior run id and what is missing.
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

    _assert_not_already_continued(record, prior_id)

    prior_iteration = prior_iteration_from_record(record)
    _assert_iteration_cap(prior_iteration, prior_id)

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

    _assert_base_is_commit(worktree, prior_id)
    _assert_no_concurrent_writer(worktree.path, prior_id)

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

    contract, contract_warnings = _load_and_pin_contract(
        prior_run_dir, record, worktree, prior_id
    )
    warnings.extend(contract_warnings)

    return record, worktree, prior_run_dir, session_meta, warnings, contract
