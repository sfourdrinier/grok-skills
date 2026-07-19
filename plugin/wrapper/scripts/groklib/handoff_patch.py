# wrapper/scripts/groklib/handoff_patch.py
#
# Phase-1 immutable git patch capture and NUL-safe changed-path listing (design §14.7).

from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
from typing import List, Optional, Sequence, Tuple

from groklib import GrokWrapperError, injectedsecrets, log_stderr, platformsupport
from groklib import path_inventory
from groklib import worktree as worktree_mod
from groklib.envelope import assert_no_secret_material, SecretMaterialError
from groklib.implementation_handoff import HandoffBlocker

_log = lambda fn, msg: log_stderr("handoff_patch", fn, msg)

_DEFAULT_PATCH_MAX = 25 * 1024 * 1024
_PATCH_FORMAT = "git-binary-full-index-v1"
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
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _assert_no_injected_denylist_in_text(text: str) -> None:
    """Fail closed when any current injected-credential denylist value occurs.

    Exact denylist occurrence is the SSOT for opaque tokens that match none of
    ``_SECRET_VALUE_PATTERNS``. Casefold matches the redaction helper so casing
    variants of the same injected credential still block the patch.
    """
    denylist = injectedsecrets.current_injected_secret_denylist()
    if not denylist or not text:
        return
    folded = text.casefold()
    for secret in denylist:
        if not secret:
            continue
        if secret in text or secret.casefold() in folded:
            raise SecretMaterialError(
                "injected credential denylist value found in patch",
                {"source": "injected-secret-denylist"},
            )


def scan_patch_bytes_for_secrets(patch_bytes: bytes) -> None:
    """Fail closed if patch bytes contain secret-shaped or injected material.

    Decodes as latin-1 (1:1 with bytes) so ASCII credentials embedded in binary
    git patch segments still match; UTF-8 replace would destroy them. Pattern
    shapes and the per-run injected denylist both apply (never instead of).
    """
    text = patch_bytes.decode("latin-1")
    _assert_no_injected_denylist_in_text(text)
    assert_no_secret_material(text)


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
        # Diff the temp index against recorded baseRevision (not live HEAD) so
        # unexpected commits on the worktree branch are still in the forensic patch.
        completed = _run_git_env(
            worktree_path,
            [
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                base_revision,
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

        # Secret scan: latin-1 is 1:1 with bytes so ASCII secret shapes inside
        # binary patch segments still match (UTF-8 replace can destroy them).
        try:
            scan_patch_bytes_for_secrets(patch_bytes)
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
    """NUL-safe changed path list vs base (tracked + untracked exclude-standard).

    Fail closed: non-(0,1) ``git diff`` or failed ``ls-files`` raises so writeScopes
    never see a silently incomplete inventory. Path decoding reuses
    path_inventory (single -z / surrogateescape source of truth); status tokens
    still come from ``diff --name-status -z``.
    """
    completed = _run_git_env(
        worktree_path,
        ["diff", "--name-status", "-z", base_revision],
    )
    if completed.returncode not in (0, 1):
        raise GrokWrapperError(
            "artifact-generation-failure",
            "git diff --name-status failed while listing changes",
            {
                "exitStatus": completed.returncode,
                "stderr": (completed.stderr or b"").decode("utf-8", errors="replace").strip(),
            },
        )
    paths: List[dict] = []
    if completed.stdout:
        parts = completed.stdout.split(b"\0")
        i = 0
        while i < len(parts):
            raw = parts[i]
            if not raw:
                i += 1
                continue
            # name-status -z: status\0path\0 or Rxxx\0old\0new\0
            # Paths are already raw -z bytes; decode only (never C-unquote).
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
    # untracked via shared path_inventory ls-files -z (no third path lister).
    for p in path_inventory.list_ls_files(
        worktree_path,
        "--others",
        "--exclude-standard",
        error_class="artifact-generation-failure",
    ):
        if not any(c["path"] == p for c in paths):
            paths.append({"path": p, "status": "added", "oldPath": None})
    return paths


