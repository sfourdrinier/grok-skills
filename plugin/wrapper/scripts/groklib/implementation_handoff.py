# wrapper/scripts/groklib/implementation_handoff.py
#
# Two-phase implementation handoff (design §14.7-14.12): schema validation,
# dual-condition ready, manifest write, primary error mapping.
# Patch capture: handoff_patch.py. Ordered finalize: code_handoff_finalize.py.

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from groklib import GrokWrapperError, log_stderr, platformsupport
from groklib.envelope import (
    assert_no_secret_material,
    redact_secret_material,
    redact_secret_value_text,
    SecretMaterialError,
)
from groklib.implementation_contract import (
    normalize_git_repo_path,
    objective_criteria_bound_errors,
)

_log = lambda fn, msg: log_stderr("implementation_handoff", fn, msg)

_PATCH_FORMAT = "git-binary-full-index-v1"
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
# Full Git object IDs only (SHA-1 = 40 hex, SHA-256 = 64 hex). No abbreviations.
_GIT_OID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_PATCH_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_CHANGE_STATUS = frozenset({"added", "modified", "deleted", "renamed"})


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_utc() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclasses.dataclass
class HandoffBlocker:
    kind: str
    message: str
    detail: Optional[dict] = None

    def as_dict(self) -> dict:
        d: Dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclasses.dataclass
class HandoffBuildResult:
    blockers: List[HandoffBlocker]
    terminal_outcome: str  # completed | failed
    manifest: Optional[dict]
    patch_path: Optional[pathlib.Path]
    primary_error_class: Optional[str]
    primary_message: Optional[str]
    step_log: List[str]


def validate_implementation_handoff(doc: dict) -> List[str]:
    """Return list of validation errors (empty if ok). Single source for writer + handoff mode.

    Structural + consistency checks for both writer and ``/grok:handoff``. When
    ``integration.ready`` is true, additional fail-closed rules apply so a
    corrupted ready=true manifest cannot be dual-condition ready.
    """
    errors: List[str] = []
    if not isinstance(doc, dict):
        return ["root must be object"]
    if doc.get("schemaVersion") != 1:
        errors.append("schemaVersion must be 1")
    run_id = doc.get("runId")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        errors.append("runId invalid")
    task_id = doc.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        errors.append("taskId must be non-empty string")
    created = doc.get("createdAtUtc")
    if not isinstance(created, str) or not created:
        errors.append("createdAtUtc must be non-empty string")
    for key in ("baseRevision", "resultTreeOid"):
        val = doc.get(key)
        if not isinstance(val, str) or not val:
            errors.append("{} must be non-empty string".format(key))
        elif not _GIT_OID_RE.match(val):
            errors.append("{} must be a full Git object id (40 or 64 hex chars)".format(key))
    patch = doc.get("patch")
    if not isinstance(patch, dict):
        errors.append("patch must be object")
    else:
        if patch.get("format") != _PATCH_FORMAT:
            errors.append("patch.format invalid")
        rel = patch.get("relativePath")
        if not isinstance(rel, str) or not rel:
            errors.append("patch.relativePath required")
        sha = patch.get("sha256")
        if not isinstance(sha, str) or not sha:
            errors.append("patch.sha256 required")
        elif not _PATCH_SHA256_RE.match(sha):
            errors.append("patch.sha256 must be 64 hex chars")
        if not isinstance(patch.get("bytes"), int) or patch.get("bytes") < 0:
            errors.append("patch.bytes must be non-negative int")
    changed = doc.get("changedFiles")
    if not isinstance(changed, list):
        errors.append("changedFiles must be array")
        changed_ok = False
    else:
        changed_ok = True
        for i, item in enumerate(changed):
            if not isinstance(item, dict):
                errors.append("changedFiles[{}] must be object".format(i))
                changed_ok = False
                continue
            p = item.get("path")
            if not isinstance(p, str) or not p:
                errors.append("changedFiles[{}].path must be non-empty string".format(i))
                changed_ok = False
            elif not _is_confined_git_repo_path(p):
                errors.append(
                    "changedFiles[{}].path must be repository-relative (no absolute or '..')".format(i)
                )
                changed_ok = False
            st = item.get("status")
            if st not in _ALLOWED_CHANGE_STATUS:
                errors.append(
                    "changedFiles[{}].status must be one of {}".format(
                        i, sorted(_ALLOWED_CHANGE_STATUS)
                    )
                )
                changed_ok = False
            old = item.get("oldPath")
            if old is not None and not isinstance(old, str):
                errors.append("changedFiles[{}].oldPath must be string or null".format(i))
                changed_ok = False
            elif isinstance(old, str) and old and not _is_confined_git_repo_path(old):
                errors.append(
                    "changedFiles[{}].oldPath must be repository-relative (no absolute or '..')".format(
                        i
                    )
                )
                changed_ok = False
            if st == "renamed" and (not isinstance(old, str) or not old):
                errors.append("changedFiles[{}].oldPath required for renamed".format(i))
                changed_ok = False
    validation = doc.get("validation")
    validation_shape_ok = True
    if not isinstance(validation, dict):
        errors.append("validation must be object")
        validation_shape_ok = False
    else:
        for vkey in ("requiredCommandsPassed", "buildGatePassed", "allPassed"):
            if vkey not in validation:
                # Allow older forensic manifests missing keys when not ready;
                # when ready=true these are required true below.
                continue
            if not isinstance(validation.get(vkey), bool):
                errors.append("validation.{} must be bool when present".format(vkey))
                validation_shape_ok = False
    integration = doc.get("integration")
    if not isinstance(integration, dict):
        errors.append("integration must be object")
    else:
        ready = integration.get("ready")
        if not isinstance(ready, bool):
            errors.append("integration.ready must be bool")
        blockers = integration.get("blockers")
        if not isinstance(blockers, list):
            errors.append("integration.blockers must be array")
        else:
            # ready=true is only valid with empty blockers, changes, and passed gates.
            if ready is True:
                if blockers:
                    errors.append("integration.ready true requires empty blockers")
                if changed_ok and isinstance(changed, list) and len(changed) < 1:
                    errors.append("integration.ready true requires non-empty changedFiles")
                # Non-empty changedFiles without a non-empty patch cannot transfer work.
                if (
                    changed_ok
                    and isinstance(changed, list)
                    and len(changed) >= 1
                    and isinstance(patch, dict)
                ):
                    pb = patch.get("bytes")
                    if not isinstance(pb, int) or pb < 1:
                        errors.append(
                            "integration.ready true requires patch.bytes > 0 when changedFiles is non-empty"
                        )
                if validation_shape_ok and isinstance(validation, dict):
                    for vkey in ("requiredCommandsPassed", "buildGatePassed", "allPassed"):
                        if validation.get(vkey) is not True:
                            errors.append(
                                "integration.ready true requires validation.{} true".format(vkey)
                            )
    worktree = doc.get("worktree")
    if not isinstance(worktree, dict):
        errors.append("worktree must be object")
    summary = doc.get("contractSummary")
    if summary is not None:
        if not isinstance(summary, dict):
            errors.append("contractSummary must be object or null")
        else:
            if not isinstance(summary.get("taskId"), str):
                errors.append("contractSummary.taskId must be string")
            if not isinstance(summary.get("objective"), str):
                errors.append("contractSummary.objective must be string")
            ac = summary.get("acceptanceCriteria")
            if not isinstance(ac, list) or not all(isinstance(c, str) for c in ac):
                errors.append("contractSummary.acceptanceCriteria must be string array")
            else:
                # Mirror contract load caps so a tampered on-disk manifest cannot
                # push multi-MB summary fields through the handoff response.
                errors.extend(
                    objective_criteria_bound_errors(
                        summary.get("objective"),
                        ac,
                        field_prefix="contractSummary.",
                    )
                )
    # Lineage (continue-run): iteration + continuesRunId both present or both absent.
    has_iteration = "iteration" in doc
    has_continues = "continuesRunId" in doc
    if has_iteration != has_continues:
        errors.append("iteration and continuesRunId must both be present or both absent")
    elif has_iteration:
        iteration = doc.get("iteration")
        continues = doc.get("continuesRunId")
        if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 2:
            errors.append("iteration must be an integer >= 2")
        if not isinstance(continues, str) or not _RUN_ID_RE.match(continues):
            errors.append("continuesRunId must be a runId-shaped string")
    return errors


def redact_contract_summary(summary: Optional[dict]) -> Optional[dict]:
    """Defense-in-depth: redact secret-shaped text in display summary fields.

    Write-side single place is ``write_manifest``; the handoff echo path also
    calls this so a tampered on-disk summary cannot leak known patterns into
    the parent response. Known-pattern secrets already fail envelopes closed;
    this neutralizes display fields cheaply when they still carry a match.
    """
    if summary is None:
        return None
    if not isinstance(summary, dict):
        return summary
    out: Dict[str, Any] = dict(summary)
    obj = out.get("objective")
    if isinstance(obj, str):
        out["objective"] = redact_secret_value_text(obj)
    ac = out.get("acceptanceCriteria")
    if isinstance(ac, list):
        out["acceptanceCriteria"] = [
            redact_secret_value_text(c) if isinstance(c, str) else c for c in ac
        ]
    return out


def _is_confined_git_repo_path(path: str) -> bool:
    """True when path is a safe repo-relative Git path (no absolute / '..' / NUL)."""
    try:
        normalize_git_repo_path(path)
        return True
    except GrokWrapperError:
        return False


def redact_handoff_blocker(blocker: dict) -> dict:
    """Deep-redact a handoff blocker dict before it is persisted or returned."""
    if not isinstance(blocker, dict):
        return {"kind": "validation-failure", "message": "invalid blocker"}
    out: Dict[str, Any] = {}
    kind = blocker.get("kind")
    out["kind"] = kind if isinstance(kind, str) else "validation-failure"
    msg = blocker.get("message")
    out["message"] = redact_secret_value_text(str(msg) if msg is not None else "")
    if "detail" in blocker and blocker["detail"] is not None:
        out["detail"] = redact_secret_material(blocker["detail"], redact_keys=True)
    return out


def redact_handoff_blockers(blockers: Sequence[Any]) -> List[dict]:
    return [redact_handoff_blocker(b if isinstance(b, dict) else {"kind": "validation-failure", "message": str(b)}) for b in blockers]


def compute_integration_ready(
    *,
    terminal_outcome: str,
    head_matches_base: bool,
    scopes_ok: bool,
    original_checkout_ok: bool,
    sentinel_ok: bool,
    patch_ok: bool,
    validation_ok: bool,
    build_gate_ok: bool,
    shared_safety_ok: bool,
    blockers: Sequence[HandoffBlocker],
    changed_count: int,
) -> bool:
    if terminal_outcome != "completed":
        return False
    if not all(
        [
            head_matches_base,
            scopes_ok,
            original_checkout_ok,
            sentinel_ok,
            patch_ok,
            validation_ok,
            build_gate_ok,
            shared_safety_ok,
        ]
    ):
        return False
    if blockers:
        return False
    if changed_count < 1:
        return False
    return True


# Evidence-backed ready (Task 7.4): ready=true is only honest when at least one
# wrapper-executed gate is marked authoritative AND commands[] carries a real
# exitStatus. Model claims never count.
NO_AUTHORITATIVE_VALIDATION_KIND = "no-authoritative-validation"
NO_AUTHORITATIVE_VALIDATION_MESSAGE = "no authoritative validation ran"


def command_evidence_supports_ready(commands: Sequence[Any]) -> bool:
    """True when commands[] has at least one entry with a real int exitStatus."""
    for cmd in commands or []:
        if not isinstance(cmd, dict):
            continue
        if "exitStatus" not in cmd:
            continue
        try:
            int(cmd["exitStatus"])
        except (TypeError, ValueError):
            continue
        return True
    return False


def authoritative_source_passed(validation: Any) -> bool:
    """True when any gate source is authoritative and passed."""
    if not isinstance(validation, dict):
        return False
    sources = validation.get("sources")
    if not isinstance(sources, dict):
        return False
    for key in ("wrapperBuildGate", "contractRequiredValidation"):
        src = sources.get(key)
        if (
            isinstance(src, dict)
            and src.get("authoritative") is True
            and src.get("passed") is True
        ):
            return True
    return False


def ready_evidence_guard_errors(
    manifest: dict, commands: Sequence[Any]
) -> List[str]:
    """Return errors when integration.ready is true without non-forgeable evidence.

    Fail-closed: ready=true requires (1) an authoritative validation source that
    passed and (2) a commands[] entry with a real exitStatus. Used by the
    forgery guard and unit tests that attempt to force ready without evidence.
    """
    if not isinstance(manifest, dict):
        return ["manifest must be object"]
    integration = manifest.get("integration")
    if not isinstance(integration, dict) or integration.get("ready") is not True:
        return []
    errors: List[str] = []
    if not authoritative_source_passed(manifest.get("validation")):
        errors.append(
            "integration.ready true requires an authoritative validation source that passed"
        )
    if not command_evidence_supports_ready(commands):
        errors.append(
            "integration.ready true requires a commands[] entry with a real exitStatus"
        )
    return errors


def enforce_ready_evidence_guard(
    manifest: dict, commands: Sequence[Any]
) -> dict:
    """Force ready=false + blocker when ready lacks non-forgeable evidence.

    Mutates ``manifest`` in place and returns it. Never synthesizes command
    evidence; only downgrades a forged/vacuous ready claim.
    """
    errors = ready_evidence_guard_errors(manifest, commands)
    if not errors:
        return manifest
    integration = manifest.get("integration")
    if not isinstance(integration, dict):
        integration = {}
        manifest["integration"] = integration
    integration["ready"] = False
    blockers: List[Any] = []
    raw = integration.get("blockers")
    if isinstance(raw, list):
        blockers = list(raw)
    if not any(
        isinstance(b, dict) and b.get("kind") == NO_AUTHORITATIVE_VALIDATION_KIND
        for b in blockers
    ):
        blockers.append(
            {
                "kind": NO_AUTHORITATIVE_VALIDATION_KIND,
                "message": NO_AUTHORITATIVE_VALIDATION_MESSAGE,
                "detail": {"errors": list(errors)},
            }
        )
    integration["blockers"] = blockers
    return manifest


# git-binary-full-index-v1 (and plain unified) path headers.
_DIFF_GIT_A_B = re.compile(
    rb"^diff --git a/(.+?) b/(.+?)\s*$",
    re.MULTILINE,
)


def _unquote_git_path(raw: str) -> str:
    """Strip optional surrounding double quotes from a git path token."""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def paths_from_git_patch(patch_bytes: bytes) -> set:
    """Extract repo-relative paths named by ``diff --git a/... b/...`` headers."""
    found: set = set()
    for match in _DIFF_GIT_A_B.finditer(patch_bytes):
        for group in (match.group(1), match.group(2)):
            try:
                text = group.decode("utf-8", errors="surrogateescape")
            except Exception:
                continue
            path = _unquote_git_path(text)
            if path and path != "/dev/null":
                found.add(path)
    return found


def paths_from_manifest_changed(changed: object) -> set:
    """Destination + rename source paths from manifest changedFiles entries."""
    found: set = set()
    if not isinstance(changed, list):
        return found
    for item in changed:
        if not isinstance(item, dict):
            continue
        p = item.get("path")
        if isinstance(p, str) and p:
            found.add(p)
        old = item.get("oldPath")
        if isinstance(old, str) and old:
            found.add(old)
    return found


def destination_paths_from_manifest_changed(changed: object) -> set:
    """Destination paths only (matches code envelope changedFiles list)."""
    found: set = set()
    if not isinstance(changed, list):
        return found
    for item in changed:
        if not isinstance(item, dict):
            continue
        p = item.get("path")
        if isinstance(p, str) and p:
            found.add(p)
    return found


def dual_condition_ready(
    *,
    manifest: Optional[dict],
    envelope: Optional[dict],
    patch_abs: Optional[pathlib.Path],
) -> Tuple[bool, List[dict]]:
    """Observed ready for /grok:handoff: valid ready manifest + success envelope + rehash.

    Also cross-checks manifest ``changedFiles`` against paths derived from the
    patch headers and (when present) the code envelope's ``changedFiles`` list so
    a corrupted ready manifest cannot point parents at the wrong dirty-overlap set.
    """
    blockers: List[dict] = []
    if not manifest:
        blockers.append({"kind": "handoff-unavailable", "message": "no handoff manifest"})
        return False, blockers
    errs = validate_implementation_handoff(manifest)
    if errs:
        blockers.append(
            {"kind": "handoff-unavailable", "message": "invalid handoff manifest", "detail": {"errors": errs}}
        )
        return False, blockers
    if not manifest.get("integration", {}).get("ready"):
        blockers.extend(list(manifest.get("integration", {}).get("blockers") or []))
        if not blockers:
            blockers.append({"kind": "not-ready", "message": "integration.ready is false"})
        return False, blockers
    if not envelope or envelope.get("status") != "success":
        blockers.append(
            {
                "kind": "terminal-envelope-incomplete",
                "message": "completed terminal envelope required for integration-ready handoff",
            }
        )
        return False, blockers
    if envelope.get("runId") != manifest.get("runId"):
        blockers.append({"kind": "handoff-unavailable", "message": "runId mismatch"})
        return False, blockers
    # Dual-condition ready requires a terminal CODE envelope, not any success
    # envelope that merely reuses the same runId (corrupt/replaced artifact).
    # Handoff is code-mode only (handoff.py refuses non-code runs before this
    # gate); peer runs integrate through peer-stop directly, never through here.
    env_mode = envelope.get("mode")
    if env_mode != "code":
        blockers.append(
            {
                "kind": "terminal-envelope-incomplete",
                "message": (
                    "integration-ready handoff requires a success envelope with "
                    "mode code"
                ),
                "detail": {"envelopeMode": env_mode},
            }
        )
        return False, blockers
    env_base = envelope.get("baseRevision")
    man_base = manifest.get("baseRevision")
    # Require a non-empty envelope baseRevision equal to the manifest base.
    # Null/missing envelope base is not a match (corrupt/incomplete terminal evidence).
    if not isinstance(env_base, str) or not env_base:
        blockers.append(
            {
                "kind": "terminal-envelope-incomplete",
                "message": (
                    "integration-ready handoff requires a non-empty envelope "
                    "baseRevision matching the handoff manifest"
                ),
                "detail": {"envelopeBase": env_base, "manifestBase": man_base},
            }
        )
        return False, blockers
    if not isinstance(man_base, str) or not man_base or env_base != man_base:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "envelope baseRevision does not match handoff manifest",
                "detail": {"envelopeBase": env_base, "manifestBase": man_base},
            }
        )
        return False, blockers
    rel = manifest.get("patch", {}).get("relativePath")
    expected = manifest.get("patch", {}).get("sha256")
    expected_bytes = manifest.get("patch", {}).get("bytes")
    if not patch_abs or not patch_abs.is_file():
        blockers.append({"kind": "artifact-integrity-failure", "message": "patch file missing"})
        return False, blockers
    try:
        actual_size = patch_abs.stat().st_size
    except OSError:
        blockers.append({"kind": "artifact-integrity-failure", "message": "patch file unreadable"})
        return False, blockers
    if not isinstance(expected_bytes, int) or expected_bytes < 1 or actual_size < 1:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "ready handoff requires a non-empty implementation patch",
                "detail": {"manifestBytes": expected_bytes, "fileBytes": actual_size},
            }
        )
        return False, blockers
    if actual_size != expected_bytes:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "patch byte size does not match handoff manifest",
                "detail": {"expected": expected_bytes, "actual": actual_size},
            }
        )
        return False, blockers
    try:
        patch_bytes = patch_abs.read_bytes()
    except OSError:
        blockers.append({"kind": "artifact-integrity-failure", "message": "patch file unreadable"})
        return False, blockers
    actual = hashlib.sha256(patch_bytes).hexdigest()
    if actual != expected:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "patch sha256 mismatch",
                "detail": {"expected": expected, "actual": actual},
            }
        )
        return False, blockers

    # Cross-check path sets: corrupted ready manifests must not pass dual-condition
    # when changedFiles names different files than the hashed patch (or envelope).
    man_all = paths_from_manifest_changed(manifest.get("changedFiles"))
    man_dest = destination_paths_from_manifest_changed(manifest.get("changedFiles"))
    patch_paths = paths_from_git_patch(patch_bytes)
    if not man_all:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "ready handoff requires parseable changedFiles paths",
            }
        )
        return False, blockers
    if not patch_paths:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "could not derive changed paths from implementation patch headers",
            }
        )
        return False, blockers
    if man_all != patch_paths:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "manifest changedFiles does not match paths in implementation patch",
                "detail": {
                    "manifestPaths": sorted(man_all),
                    "patchPaths": sorted(patch_paths),
                },
            }
        )
        return False, blockers
    env_cf = envelope.get("changedFiles")
    if isinstance(env_cf, list) and env_cf:
        env_paths = {p for p in env_cf if isinstance(p, str) and p}
        if env_paths != man_dest:
            blockers.append(
                {
                    "kind": "artifact-integrity-failure",
                    "message": "envelope changedFiles does not match handoff manifest destinations",
                    "detail": {
                        "envelopePaths": sorted(env_paths),
                        "manifestDestinations": sorted(man_dest),
                    },
                }
            )
            return False, blockers
    return True, []


def write_manifest(path: pathlib.Path, doc: dict) -> None:
    """Validate, secret-redact blockers + contractSummary, write JSON (mode 0600).

    Write-side redaction for contractSummary lives HERE only (not also in
    code_handoff_finalize): one place for objective/criteria display fields.
    """
    # Never persist raw secret-shaped argv/details from operator validation failures.
    if isinstance(doc, dict):
        doc = dict(doc)
        integration = doc.get("integration")
        if isinstance(integration, dict) and isinstance(integration.get("blockers"), list):
            integration = dict(integration)
            integration["blockers"] = redact_handoff_blockers(integration["blockers"])
            doc["integration"] = integration
        # Defense-in-depth for display fields (Phase 1 finding 4).
        if "contractSummary" in doc:
            doc["contractSummary"] = redact_contract_summary(doc.get("contractSummary"))
    errs = validate_implementation_handoff(doc)
    if errs:
        raise GrokWrapperError(
            "artifact-generation-failure",
            "handoff manifest failed validation before write",
            {"errors": errs},
        )
    # Fail closed if residual secret-shaped material survived blocker redaction
    # (taskId, paths, or opaque values that match known patterns).
    try:
        assert_no_secret_material(doc)
    except SecretMaterialError as exc:
        raise GrokWrapperError(
            "artifact-generation-failure",
            "handoff manifest contains secret-shaped material: {}".format(exc),
            dict(exc.detail or {}),
        ) from exc
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
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


# Hard policy blockers fail the code envelope (raise after handoff write).
# Ready-only soft kinds (no-changes, temp-index-retained) never become primary.
HARD_BLOCKER_KINDS = frozenset(
    {
        "write-scope-violation",
        "unexpected-commit",
        "secret-material",
        "artifact-too-large",
        "artifact-generation-failure",
        "validation-failure",
        "wrong-working-directory",
        "unexpected-edits",
        "sandbox-failure",
        "worktree-failure",
    }
)

# Soft blockers only force integration.ready false (never primary ERROR_CLASS alone).
SOFT_BLOCKER_KINDS = frozenset(
    {
        "no-changes",
        "temp-index-retained",
        "no-authoritative-validation",
        "handoff-unavailable",
    }
)

_HARD_PRIMARY_MAPPING = {
    "write-scope-violation": "write-scope-violation",
    "unexpected-commit": "unexpected-commit",
    "secret-material": "artifact-generation-failure",
    "artifact-too-large": "artifact-generation-failure",
    "artifact-generation-failure": "artifact-generation-failure",
    "validation-failure": "validation-failure",
    "wrong-working-directory": "wrong-working-directory",
    "unexpected-edits": "unexpected-edits",
    "sandbox-failure": "sandbox-failure",
    "worktree-failure": "worktree-failure",
}


def primary_error_from_blockers(blockers: Sequence[HandoffBlocker]) -> Tuple[Optional[str], Optional[str]]:
    """Map the first *hard* policy blocker to envelope ERROR_CLASS.

    Skips ready-only soft kinds (``no-changes``, ``temp-index-retained``) so a
    soft blocker earlier in the list cannot steal primary class from a later
    hard failure (e.g. unexpected-edits with phase=post-build-gate).

    Unknown kinds (not soft) fail closed as ``artifact-generation-failure``.
    """
    for b in blockers:
        if b.kind in SOFT_BLOCKER_KINDS:
            continue
        if b.kind in HARD_BLOCKER_KINDS:
            cls = _HARD_PRIMARY_MAPPING.get(b.kind)
            if cls:
                return cls, b.message
            return "artifact-generation-failure", b.message
        # Unknown kind: hard fail-closed (do not treat as soft).
        return (
            "artifact-generation-failure",
            b.message or "unknown handoff blocker kind: {}".format(b.kind),
        )
    return None, None

# ---------------------------------------------------------------------------
# Ordered post-Grok finalization (design §14.6) - single entry for code mode
# ---------------------------------------------------------------------------

_STEP_ORDER = (
    "verify-sentinel",
    "remove-sentinel",
    "head-check",
    "changed-files",
    "write-scopes",
    "forensic-patch",
    "required-validation",
    "build-gate",
    "forensic-patch-post-gate",
    "shared-safety",
    "terminal-outcome",
    "compute-ready",
    "write-manifest",
)

