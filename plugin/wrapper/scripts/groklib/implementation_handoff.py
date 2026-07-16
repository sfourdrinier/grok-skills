# wrapper/scripts/groklib/implementation_handoff.py
#
# Two-phase implementation handoff (design §14.7–14.12): immutable git patch +
# final manifest. Single validate_implementation_handoff for writer and handoff mode.

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


def capture_phase1_patch(
    *,
    worktree_path: pathlib.Path,
    base_revision: str,
    artifacts_dir: pathlib.Path,
    run_id: str,
) -> Tuple[Optional[dict], Optional[pathlib.Path], Optional[str], List[HandoffBlocker], List[str]]:
    """Phase-1: immutable binary full-index patch under artifacts/. Returns patch meta, path, tree oid, blockers, steps."""
    steps: List[str] = []
    blockers: List[HandoffBlocker] = []
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    try:
        platformsupport.restrict_dir_permissions(artifacts_dir)
    except OSError:
        pass

    token = secrets.token_hex(4)
    tmp_index = artifacts_dir / "handoff.{}.{}.idx".format(os.getpid(), token)
    patch_path = artifacts_dir / "implementation.patch"
    result_tree: Optional[str] = None
    patch_meta: Optional[dict] = None

    child_env = dict(os.environ)
    child_env["GIT_INDEX_FILE"] = str(tmp_index)
    # Ensure empty index
    if tmp_index.exists():
        try:
            tmp_index.unlink()
        except OSError:
            pass

    try:
        steps.append("phase1-read-tree")
        _git_ok(worktree_path, ["read-tree", base_revision], env=child_env)
        steps.append("phase1-add")
        # Stage tracked + untracked non-ignored (match code changed set; exclude standard ignored)
        _git_ok(worktree_path, ["add", "-A"], env=child_env)
        steps.append("phase1-write-tree")
        result_tree = _git_ok(worktree_path, ["write-tree"], env=child_env).strip()
        steps.append("phase1-diff")
        completed = _run_git_env(
            worktree_path,
            [
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
            ],
            env=child_env,
        )
        if completed.returncode not in (0, 1):
            raise GrokWrapperError(
                "artifact-generation-failure",
                "git diff --cached failed",
                {
                    "stderr": (completed.stderr or b"").decode("utf-8", errors="replace").strip(),
                    "exitStatus": completed.returncode,
                },
            )
        patch_bytes = completed.stdout or b""
        max_b = _patch_max_bytes()
        if len(patch_bytes) > max_b:
            blockers.append(
                HandoffBlocker(
                    "artifact-too-large",
                    "implementation patch exceeds max size",
                    {"bytes": len(patch_bytes), "maxBytes": max_b},
                )
            )
            return None, None, result_tree, blockers, steps

        # Secret scan on text form
        try:
            assert_no_secret_material(patch_bytes.decode("utf-8", errors="replace"))
        except SecretMaterialError as exc:
            blockers.append(
                HandoffBlocker("secret-material", "secret-shaped material in patch", {"error": str(exc)})
            )
            return None, None, result_tree, blockers, steps

        steps.append("phase1-write-patch")
        fd = os.open(str(patch_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(patch_bytes)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        try:
            os.chmod(str(patch_path), 0o600)
        except OSError:
            pass
        digest = _sha256_file(patch_path)
        verify = patch_path.read_bytes()
        if _sha256_bytes(verify) != digest:
            raise GrokWrapperError(
                "artifact-generation-failure",
                "patch re-read sha256 mismatch after write",
                {},
            )
        patch_meta = {
            "format": _PATCH_FORMAT,
            "relativePath": "artifacts/implementation.patch",
            "sha256": digest,
            "bytes": len(patch_bytes),
        }
    except GrokWrapperError as exc:
        blockers.append(
            HandoffBlocker(
                "artifact-generation-failure",
                str(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )
        patch_meta = None
        patch_path_out = None
    except OSError as exc:
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "patch capture failed: {}".format(exc), {})
        )
        patch_meta = None
        patch_path_out = None
    else:
        patch_path_out = patch_path if patch_meta else None
    finally:
        # Temp index cleanup with post-check (§14.7)
        delete_err = None
        try:
            if tmp_index.exists():
                tmp_index.unlink()
        except OSError as exc:
            delete_err = exc
        if tmp_index.exists():
            blockers.append(
                HandoffBlocker(
                    "temp-index-retained",
                    "temp git index could not be removed",
                    {"path": str(tmp_index), "error": str(delete_err) if delete_err else None},
                )
            )
            steps.append("phase1-temp-index-retained")
        elif delete_err is not None:
            steps.append("phase1-temp-index-delete-err-but-gone")
            _log("capture_phase1_patch", "temp index delete raised but path absent: {}".format(delete_err))
        else:
            steps.append("phase1-temp-index-cleaned")

    return patch_meta, patch_path_out, result_tree, blockers, steps


def list_changed_paths(worktree_path: pathlib.Path, base_revision: str) -> List[dict]:
    """NUL-safe changed path list vs base (tracked + untracked exclude-standard)."""
    completed = _run_git_env(
        worktree_path,
        ["diff", "--name-status", "-z", base_revision],
    )
    paths: List[dict] = []
    if completed.returncode in (0, 1) and completed.stdout:
        parts = completed.stdout.split(b"\0")
        i = 0
        while i < len(parts):
            raw = parts[i]
            if not raw:
                i += 1
                continue
            # name-status -z: status\0path\0 or Rxxx\0old\0new\0
            try:
                status = raw.decode("utf-8", errors="surrogateescape")
            except Exception:
                i += 1
                continue
            if status.startswith("R") or status.startswith("C"):
                if i + 2 >= len(parts):
                    break
                old_p = parts[i + 1].decode("utf-8", errors="surrogateescape")
                new_p = parts[i + 2].decode("utf-8", errors="surrogateescape")
                paths.append({"path": new_p, "status": "renamed", "oldPath": old_p})
                i += 3
            else:
                if i + 1 >= len(parts):
                    break
                p = parts[i + 1].decode("utf-8", errors="surrogateescape")
                st = "modified"
                if status.startswith("A"):
                    st = "added"
                elif status.startswith("D"):
                    st = "deleted"
                paths.append({"path": p, "status": st, "oldPath": None})
                i += 2
    # untracked
    ut = _run_git_env(worktree_path, ["ls-files", "-z", "--others", "--exclude-standard"])
    if ut.returncode == 0 and ut.stdout:
        for raw in ut.stdout.split(b"\0"):
            if not raw:
                continue
            p = raw.decode("utf-8", errors="surrogateescape")
            if not any(c["path"] == p for c in paths):
                paths.append({"path": p, "status": "added", "oldPath": None})
    return paths


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


def primary_error_from_blockers(blockers: Sequence[HandoffBlocker]) -> Tuple[Optional[str], Optional[str]]:
    """Map first hard blocker to envelope ERROR_CLASS."""
    mapping = {
        "write-scope-violation": "write-scope-violation",
        "unexpected-commit": "unexpected-commit",
        "secret-material": "artifact-generation-failure",
        "artifact-too-large": "artifact-generation-failure",
        "artifact-generation-failure": "artifact-generation-failure",
        "temp-index-retained": "artifact-generation-failure",
        "validation-failure": "validation-failure",
        "no-changes": "validation-failure",
        "wrong-working-directory": "wrong-working-directory",
        "unexpected-edits": "unexpected-edits",
        "sandbox-failure": "sandbox-failure",
        "worktree-failure": "worktree-failure",
    }
    for b in blockers:
        cls = mapping.get(b.kind)
        if cls:
            return cls, b.message
    if blockers:
        return "validation-failure", blockers[0].message
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
# Ordered post-Grok finalization (design §14.6) — single entry for code mode
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


def _head_sha(worktree_path: pathlib.Path) -> str:
    return _git_ok(worktree_path, ["rev-parse", "HEAD"]).strip()


def _remove_exact_sentinel(worktree_path: pathlib.Path, sentinel_name: str) -> None:
    """Remove only the exact sentinel regular file; never a similar user path."""
    path = worktree_path / sentinel_name
    try:
        st = path.lstat()
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise GrokWrapperError(
            "wrong-working-directory",
            "cwd sentinel is not a regular file and cannot be removed safely",
            {"sentinel": sentinel_name, "path": str(path)},
        )
    try:
        path.unlink()
    except OSError as exc:
        raise GrokWrapperError(
            "wrong-working-directory",
            "could not remove cwd sentinel: {}".format(exc),
            {"sentinel": sentinel_name},
        ) from exc


def _contract_sha256(contract: Optional[dict]) -> Optional[str]:
    if not contract:
        return None
    # Stable hash of normalized contract content (not file path)
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def code_handoff_finalize(
    *,
    stage: Any,
    sentinel_name: str,
    contract: Optional[dict],
    artifacts_dir: pathlib.Path,
    original_baseline: Any,
    run_build_gate: Callable[..., None],
    assert_changes_within: Callable[..., None],
    assert_original_checkout_unmodified: Callable[..., None],
    assert_cwd_sentinel: Callable[..., None],
    run_recorded_command: Callable[..., dict],
    step_log: Optional[List[str]] = None,
) -> HandoffBuildResult:
    """Execute design §14.6 order on the FinalizeStage path. Writes handoff before return/raise.

    Policy failures accumulate as blockers and continue forensics when safe.
    After writing the phase-2 manifest, raises primary GrokWrapperError when
    terminalOutcome is failed (so the worktree runner emits a failure envelope).
    """
    from groklib import worktree as worktree_mod

    steps: List[str] = list(step_log) if step_log is not None else []
    blockers: List[HandoffBlocker] = []
    worktree = stage.worktree
    base_revision = worktree.base_revision
    run_id = stage.run_id
    scopes = list((contract or {}).get("writeScopes") or [])
    task_id = (contract or {}).get("taskId") or "no-contract"

    head_ok = True
    scopes_ok = True
    sentinel_ok = True
    patch_ok = False
    validation_ok = True
    build_gate_ok = True
    shared_safety_ok = True
    original_checkout_ok = True
    patch_meta: Optional[dict] = None
    patch_path: Optional[pathlib.Path] = None
    result_tree: Optional[str] = None
    changed: List[dict] = []
    validation_evidence: List[dict] = []

    # 1. verify sentinel (hard fail — no spoofed workspace)
    steps.append("verify-sentinel")
    try:
        assert_cwd_sentinel(worktree, sentinel_name)
    except GrokWrapperError:
        sentinel_ok = False
        raise

    # 2. remove exact sentinel only
    steps.append("remove-sentinel")
    _remove_exact_sentinel(worktree.path, sentinel_name)

    # 3. HEAD still equals baseRevision
    steps.append("head-check")
    try:
        head = _head_sha(worktree.path)
        if head != base_revision:
            head_ok = False
            blockers.append(
                HandoffBlocker(
                    "unexpected-commit",
                    "worktree HEAD moved from baseRevision during the run",
                    {"head": head, "baseRevision": base_revision},
                )
            )
            _log("code_handoff_finalize", "unexpected-commit head={} base={}".format(head, base_revision))
    except GrokWrapperError as exc:
        head_ok = False
        blockers.append(
            HandoffBlocker("unexpected-commit", "could not read HEAD: {}".format(exc), {})
        )

    # 4. changed files (sentinel must not appear)
    steps.append("changed-files")
    try:
        changed = list_changed_paths(worktree.path, base_revision)
        # Filter any residual sentinel name
        changed = [c for c in changed if c.get("path") != sentinel_name]
        # Envelope uses path strings
        stage.acc.changed_files = [c["path"] for c in changed]
        try:
            _summary_files, diff_text = worktree_mod.diff_summary(worktree)
            stage.acc.diff_summary = diff_text
        except Exception as exc:
            _log("code_handoff_finalize", "diff_summary failed: {}".format(exc))
        stage.acc.effective_working_directory = str(worktree.path)
    except Exception as exc:
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "could not list changed files: {}".format(exc), {})
        )
        changed = []

    # 5. write scopes (contract)
    steps.append("write-scopes")
    if contract and scopes:
        for entry in changed:
            p = entry.get("path") or ""
            if not path_in_scopes(p, scopes):
                scopes_ok = False
                blockers.append(
                    HandoffBlocker(
                        "write-scope-violation",
                        "changed path outside writeScopes",
                        {"path": p},
                    )
                )

    # Confinement scan of worktree changes (pre-build-gate; original checkout
    # re-scan is after the gate so post-build-gate phase is preserved).
    try:
        assert_changes_within(worktree, (worktree.path,), original_baseline=original_baseline)
    except GrokWrapperError as exc:
        shared_safety_ok = False
        kind = exc.error_class if exc.error_class in (
            "unexpected-edits",
            "sandbox-failure",
            "worktree-failure",
        ) else "validation-failure"
        blockers.append(
            HandoffBlocker(
                kind,
                "worktree escape / confinement failed: {}".format(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )

    # 6. phase-1 forensic patch
    steps.append("forensic-patch")
    try:
        patch_meta, patch_path, result_tree, patch_blockers, patch_steps = capture_phase1_patch(
            worktree_path=worktree.path,
            base_revision=base_revision,
            artifacts_dir=artifacts_dir,
            run_id=run_id,
        )
        steps.extend(patch_steps)
        blockers.extend(patch_blockers)
        fatal_patch = {
            "secret-material",
            "artifact-too-large",
            "artifact-generation-failure",
        }
        patch_ok = patch_meta is not None and not any(b.kind in fatal_patch for b in patch_blockers)
    except Exception as exc:
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "patch capture raised: {}".format(exc), {})
        )
        patch_ok = False

    # 7. requiredValidation (operator-trusted)
    steps.append("required-validation")
    if contract and contract.get("requiredValidation"):
        for entry in contract["requiredValidation"]:
            argv = list(entry["argv"])
            rel_cwd = entry.get("cwd") or "."
            if rel_cwd in (".", "./", ""):
                cwd = worktree.path
            else:
                try:
                    rel = normalize_repo_relative(rel_cwd)
                except GrokWrapperError as exc:
                    validation_ok = False
                    blockers.append(
                        HandoffBlocker("validation-failure", "invalid validation cwd", {"error": str(exc)})
                    )
                    continue
                cwd = (worktree.path / rel).resolve()
                try:
                    cwd.relative_to(worktree.path.resolve())
                except ValueError:
                    validation_ok = False
                    blockers.append(
                        HandoffBlocker(
                            "validation-failure",
                            "validation cwd escapes worktree",
                            {"cwd": str(cwd)},
                        )
                    )
                    continue
            rec = run_recorded_command(argv, cwd, entry.get("purpose") or "contract-validation")
            if "stdoutSha256" not in rec:
                rec = {
                    **rec,
                    **build_command_evidence(
                        argv=argv,
                        cwd=str(cwd),
                        purpose=entry.get("purpose") or "contract-validation",
                        exit_status=int(rec.get("exitStatus", 1)),
                    ),
                }
            stage.acc.commands.append(rec)
            validation_evidence.append(rec)
            try:
                assert_original_checkout_unmodified(
                    worktree, (worktree.path,), original_baseline=original_baseline
                )
            except GrokWrapperError as exc:
                validation_ok = False
                original_checkout_ok = False
                blockers.append(
                    HandoffBlocker(
                        "validation-failure",
                        "original checkout modified after requiredValidation",
                        {"error": str(exc)},
                    )
                )
            if int(rec.get("exitStatus", 1)) != 0:
                validation_ok = False
                blockers.append(
                    HandoffBlocker(
                        "validation-failure",
                        "requiredValidation command failed",
                        {"argv": argv, "exitStatus": rec.get("exitStatus")},
                    )
                )
    else:
        validation_ok = True  # no contract validations

    # 8. wrapper build gate
    steps.append("build-gate")
    try:
        run_build_gate()
        build_gate_ok = True
    except GrokWrapperError as exc:
        build_gate_ok = False
        blockers.append(
            HandoffBlocker(
                "validation-failure",
                "build gate failed: {}".format(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )

    # 9. shared safety — post-build-gate original-checkout re-scan (phase tag)
    steps.append("shared-safety")
    try:
        assert_original_checkout_unmodified(
            worktree, (worktree.path,), original_baseline=original_baseline
        )
    except GrokWrapperError as exc:
        original_checkout_ok = False
        shared_safety_ok = False
        kind = exc.error_class if exc.error_class in (
            "unexpected-edits",
            "sandbox-failure",
            "worktree-failure",
        ) else "validation-failure"
        blockers.append(
            HandoffBlocker(
                kind,
                "original checkout modified after run: {}".format(exc),
                dict(exc.detail or {}, errorClass=exc.error_class),
            )
        )

    # no-changes
    if not changed:
        blockers.append(HandoffBlocker("no-changes", "no changed files to hand off", {}))

    # 10. terminalOutcome
    steps.append("terminal-outcome")
    # Ready-only blockers (do not fail the code envelope by themselves):
    # no-changes, temp-index-retained — block integration.ready only (§14.7/§14.12).
    # Hard policy failures raise after handoff write so the runner emits failure.
    hard_kinds = {
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
    policy_fail = [b for b in blockers if b.kind in hard_kinds]
    terminal_outcome = "failed" if policy_fail else "completed"

    # 11. compute ready
    steps.append("compute-ready")
    ready = compute_integration_ready(
        terminal_outcome=terminal_outcome,
        head_matches_base=head_ok,
        scopes_ok=scopes_ok if contract else True,
        original_checkout_ok=original_checkout_ok,
        sentinel_ok=sentinel_ok,
        patch_ok=patch_ok and patch_meta is not None,
        validation_ok=validation_ok,
        build_gate_ok=build_gate_ok,
        shared_safety_ok=shared_safety_ok,
        blockers=blockers,
        changed_count=len(changed),
    )

    # Ensure result tree
    if not result_tree:
        try:
            result_tree = _git_ok(worktree.path, ["rev-parse", "HEAD^{tree}"]).strip()
        except GrokWrapperError:
            result_tree = base_revision  # placeholder; validation may fail

    if patch_meta is None:
        # Minimal stub so manifest validates structure when patch failed
        patch_meta = {
            "format": _PATCH_FORMAT,
            "relativePath": "artifacts/implementation.patch",
            "sha256": "0" * 64,
            "bytes": 0,
        }
        # If no real patch, force not ready
        ready = False
        if not any(b.kind.startswith("artifact") or b.kind == "secret-material" for b in blockers):
            blockers.append(
                HandoffBlocker("artifact-generation-failure", "no implementation patch produced", {})
            )
            terminal_outcome = "failed"
            ready = False

    validation_block = {
        "requiredCommandsPassed": validation_ok,
        "buildGatePassed": build_gate_ok,
        "allPassed": validation_ok and build_gate_ok,
        "sources": {
            "wrapperBuildGate": {"authoritative": True, "passed": build_gate_ok},
            "contractRequiredValidation": {
                "authoritative": True,
                "passed": validation_ok,
                "trustModel": trust_model(),
            },
            "modelClaimedCommands": {
                "authoritative": False,
                "note": "ignored for readiness",
            },
        },
    }

    doc = {
        "schemaVersion": 1,
        "runId": run_id,
        "taskId": task_id,
        "contractSha256": _contract_sha256(contract),
        "baseRevision": base_revision,
        "resultTreeOid": result_tree or "",
        "changedFiles": changed,
        "patch": patch_meta,
        "validation": validation_block,
        "integration": {
            "ready": bool(ready),
            "blockers": [b.as_dict() for b in blockers],
        },
        "worktree": {
            "retained": True,
            "path": str(worktree.path),
            "branch": worktree.branch,
        },
        "createdAtUtc": _now_utc(),
    }

    # 12. write handoff JSON
    steps.append("write-manifest")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    try:
        platformsupport.restrict_dir_permissions(artifacts_dir)
    except OSError:
        pass
    manifest_path = artifacts_dir.parent / "implementation-handoff.json"
    # Design places implementation-handoff.json at run root next to artifacts/
    # Prefer run_dir/implementation-handoff.json
    run_dir = artifacts_dir.parent
    manifest_path = run_dir / "implementation-handoff.json"
    try:
        write_manifest(manifest_path, doc)
    except GrokWrapperError as exc:
        # If validation fails due to empty resultTree etc., try to still record
        _log("code_handoff_finalize", "manifest write failed: {}".format(exc))
        blockers.append(
            HandoffBlocker("artifact-generation-failure", "manifest write failed: {}".format(exc), {})
        )
        terminal_outcome = "failed"
        ready = False
        doc["integration"]["ready"] = False
        doc["integration"]["blockers"] = [b.as_dict() for b in blockers]
        # Force minimal valid fields
        if not doc.get("resultTreeOid"):
            doc["resultTreeOid"] = "0" * 40
        try:
            write_manifest(manifest_path, doc)
        except Exception as inner:
            _log("code_handoff_finalize", "second manifest write failed: {}".format(inner))

    primary_class, primary_msg = primary_error_from_blockers(blockers)
    result = HandoffBuildResult(
        blockers=blockers,
        terminal_outcome=terminal_outcome,
        manifest=doc,
        patch_path=patch_path,
        primary_error_class=primary_class,
        primary_message=primary_msg,
        step_log=steps,
    )

    # After handoff write: raise primary so runner emits failure envelope
    if terminal_outcome == "failed" and primary_class:
        # Prefer the first hard blocker's own detail (e.g. phase=post-build-gate,
        # violations[]) so envelope.error.detail matches pre-PR4 callers.
        primary_detail: Dict[str, Any] = {}
        for b in blockers:
            mapped = {
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
            }.get(b.kind)
            if mapped == primary_class:
                if b.detail:
                    primary_detail.update(b.detail)
                break
        primary_detail["blockers"] = [b.as_dict() for b in blockers]
        primary_detail["stepLog"] = steps
        raise GrokWrapperError(
            primary_class, primary_msg or "implementation handoff not ready", primary_detail
        )

    return result
