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
import secrets
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from groklib import GrokWrapperError, log_stderr, platformsupport
from groklib import worktree as worktree_mod
from groklib.command_evidence import build_command_evidence
from groklib.envelope import assert_no_secret_material, SecretMaterialError
from groklib.implementation_contract import (
    normalize_repo_relative,
    path_in_scopes,
    trust_model,
)

_log = lambda fn, msg: log_stderr("implementation_handoff", fn, msg)

_DEFAULT_PATCH_MAX = 25 * 1024 * 1024
_PATCH_FORMAT = "git-binary-full-index-v1"
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_EMPTY_HOOKS = worktree_mod._EMPTY_GIT_HOOKS


def _patch_max_bytes() -> int:
    raw = (os.environ.get("GROK_HANDOFF_PATCH_MAX_BYTES") or "").strip()
    if not raw:
        return _DEFAULT_PATCH_MAX
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_PATCH_MAX
    return max(1 * 1024 * 1024, min(n, 100 * 1024 * 1024))


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_utc() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _run_git_env(repo: pathlib.Path, args: Sequence[str], env: Optional[dict] = None) -> subprocess.CompletedProcess:
    child = dict(os.environ if env is None else env)
    argv = ["git", "-c", "core.hooksPath={}".format(_EMPTY_HOOKS), "-C", str(repo)] + [
        str(a) for a in args
    ]
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child,
        check=False,
    )


def _git_ok(repo: pathlib.Path, args: Sequence[str], env: Optional[dict] = None) -> str:
    completed = _run_git_env(repo, args, env=env)
    if completed.returncode != 0:
        raise GrokWrapperError(
            "artifact-generation-failure",
            "git {} failed".format(" ".join(str(a) for a in args)),
            {
                "stderr": (completed.stderr or b"").decode("utf-8", errors="replace").strip(),
                "exitStatus": completed.returncode,
            },
        )
    return (completed.stdout or b"").decode("utf-8", errors="replace")


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
    """Return list of validation errors (empty if ok). Single source for writer + handoff mode."""
    errors: List[str] = []
    if not isinstance(doc, dict):
        return ["root must be object"]
    if doc.get("schemaVersion") != 1:
        errors.append("schemaVersion must be 1")
    run_id = doc.get("runId")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        errors.append("runId invalid")
    for key in ("taskId", "baseRevision", "resultTreeOid", "createdAtUtc"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            errors.append("{} must be non-empty string".format(key))
    patch = doc.get("patch")
    if not isinstance(patch, dict):
        errors.append("patch must be object")
    else:
        if patch.get("format") != _PATCH_FORMAT:
            errors.append("patch.format invalid")
        for k in ("relativePath", "sha256"):
            if not isinstance(patch.get(k), str) or not patch.get(k):
                errors.append("patch.{} required".format(k))
        if not isinstance(patch.get("bytes"), int) or patch.get("bytes") < 0:
            errors.append("patch.bytes must be non-negative int")
    changed = doc.get("changedFiles")
    if not isinstance(changed, list):
        errors.append("changedFiles must be array")
    validation = doc.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be object")
    integration = doc.get("integration")
    if not isinstance(integration, dict):
        errors.append("integration must be object")
    else:
        if not isinstance(integration.get("ready"), bool):
            errors.append("integration.ready must be bool")
        if not isinstance(integration.get("blockers"), list):
            errors.append("integration.blockers must be array")
    worktree = doc.get("worktree")
    if not isinstance(worktree, dict):
        errors.append("worktree must be object")
    return errors


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


def dual_condition_ready(
    *,
    manifest: Optional[dict],
    envelope: Optional[dict],
    patch_abs: Optional[pathlib.Path],
) -> Tuple[bool, List[dict]]:
    """Observed ready for /grok:handoff: valid ready manifest + success envelope + rehash."""
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
    rel = manifest.get("patch", {}).get("relativePath")
    expected = manifest.get("patch", {}).get("sha256")
    if not patch_abs or not patch_abs.is_file():
        blockers.append({"kind": "artifact-integrity-failure", "message": "patch file missing"})
        return False, blockers
    actual = _sha256_file(patch_abs)
    if actual != expected:
        blockers.append(
            {
                "kind": "artifact-integrity-failure",
                "message": "patch sha256 mismatch",
                "detail": {"expected": expected, "actual": actual},
            }
        )
        return False, blockers
    return True, []


def write_manifest(path: pathlib.Path, doc: dict) -> None:
    errs = validate_implementation_handoff(doc)
    if errs:
        raise GrokWrapperError(
            "artifact-generation-failure",
            "handoff manifest failed validation before write",
            {"errors": errs},
        )
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
SOFT_BLOCKER_KINDS = frozenset({"no-changes", "temp-index-retained"})

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
    """
    for b in blockers:
        if b.kind not in HARD_BLOCKER_KINDS:
            continue
        cls = _HARD_PRIMARY_MAPPING.get(b.kind)
        if cls:
            return cls, b.message
    return None, None


def run_contract_validations(
    *,
    worktree_path: pathlib.Path,
    required: Sequence[dict],
    run_command: Callable[..., dict],
) -> Tuple[bool, List[dict], List[HandoffBlocker]]:
    """Execute requiredValidation entries. run_command(argv, cwd, purpose) -> evidence-like dict with exitStatus."""
    blockers: List[HandoffBlocker] = []
    evidence: List[dict] = []
    all_ok = True
    for entry in required:
        argv = list(entry["argv"])
        rel_cwd = entry.get("cwd") or "."
        if rel_cwd in (".", "./", ""):
            cwd = worktree_path
        else:
            try:
                rel = normalize_repo_relative(rel_cwd)
            except GrokWrapperError as exc:
                blockers.append(
                    HandoffBlocker("validation-failure", "invalid validation cwd", {"error": str(exc)})
                )
                all_ok = False
                continue
            cwd = (worktree_path / rel).resolve()
            try:
                cwd.relative_to(worktree_path.resolve())
            except ValueError:
                blockers.append(
                    HandoffBlocker(
                        "validation-failure",
                        "validation cwd escapes worktree",
                        {"cwd": str(cwd)},
                    )
                )
                all_ok = False
                continue
        rec = run_command(argv=argv, cwd=cwd, purpose=entry.get("purpose") or "contract-validation")
        evidence.append(rec)
        if int(rec.get("exitStatus", 1)) != 0:
            all_ok = False
            blockers.append(
                HandoffBlocker(
                    "validation-failure",
                    "requiredValidation command failed",
                    {"argv": argv, "exitStatus": rec.get("exitStatus")},
                )
            )
    return all_ok, evidence, blockers

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
    "shared-safety",
    "terminal-outcome",
    "compute-ready",
    "write-manifest",
)

