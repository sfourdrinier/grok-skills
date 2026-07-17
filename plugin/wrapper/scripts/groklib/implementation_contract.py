# wrapper/scripts/groklib/implementation_contract.py
#
# Operator-trusted implementation contract (design §14.3).
# Trust model: operator-contract-trusted-no-os-sandbox
# Content is trusted after load; path load rejects non-files and symlink escapes.

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from groklib import GrokWrapperError

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRUST_MODEL = "operator-contract-trusted-no-os-sandbox"


def trust_model() -> str:
    return _TRUST_MODEL


def _contract_error(message: str, detail: Optional[dict] = None) -> GrokWrapperError:
    return GrokWrapperError("implementation-contract-invalid", message, detail or {})


def normalize_repo_relative(path: str) -> str:
    """Normalize an operator-supplied repo-relative path (contract scopes/target).

    Treats backslash as a path separator (Windows-style operator input). Do **not**
    use this for Git-reported paths - on POSIX backslash is a valid filename char.
    """
    if path is None or not isinstance(path, str):
        raise _contract_error("path must be a non-empty string", {"path": path})
    raw = path.strip().replace("\\", "/")
    return _normalize_repo_relative_raw(raw, original=path)


def normalize_git_repo_path(path: str) -> str:
    """Normalize a Git-reported repo-relative path for scope checks.

    Only forward slash is a separator. Backslash is preserved as a literal
    character so a root file named ``pkg\\evil.ts`` is not treated as under ``pkg/``.
    Does **not** strip whitespace: leading/trailing spaces are valid filename chars.
    """
    if path is None or not isinstance(path, str):
        raise _contract_error("path must be a non-empty string", {"path": path})
    # Never strip: Git paths may begin/end with spaces and still be distinct names.
    return _normalize_repo_relative_raw(path, original=path)


def _normalize_repo_relative_raw(raw: str, *, original: str) -> str:
    if not raw or "\x00" in raw:
        raise _contract_error("path is empty or contains NUL", {"path": original})
    if raw in (".", "./"):
        return "."
    if os.path.isabs(raw) or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise _contract_error("path must be repository-relative (not absolute)", {"path": original})
    cleaned: List[str] = []
    # Split only on forward slash (never on backslash).
    for p in raw.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise _contract_error("path must not contain '..'", {"path": original})
        cleaned.append(p)
    if not cleaned:
        raise _contract_error("path resolves empty", {"path": original})
    return "/".join(cleaned)


def path_in_scopes(path: str, scopes: Sequence[dict], *, from_git: bool = False) -> bool:
    """True when path is covered by a file (exact) or subtree (component prefix) scope.

    When ``from_git`` is True, the path is treated as Git-reported (backslash is
    literal). Scope entries are always operator-normalized (slash conversion).
    """
    try:
        norm = normalize_git_repo_path(path) if from_git else normalize_repo_relative(path)
    except GrokWrapperError:
        return False
    path_parts = norm.split("/")
    for scope in scopes:
        kind = scope.get("kind")
        sp = scope.get("path")
        if not isinstance(sp, str):
            continue
        try:
            # Scopes are already normalized at contract load; re-normalize as operator paths.
            scope_norm = normalize_repo_relative(sp)
        except GrokWrapperError:
            continue
        if kind == "file":
            if norm == scope_norm:
                return True
        elif kind == "subtree":
            # Repository-root subtree: path "." covers every repo-relative path.
            if scope_norm == ".":
                return True
            scope_parts = scope_norm.split("/")
            if path_parts[: len(scope_parts)] == scope_parts:
                return True
        else:
            continue
    return False


def _is_top_level_os_alias_symlink(component: pathlib.Path, final_abs: pathlib.Path) -> bool:
    """Allow only top-level OS mount aliases (e.g. /var -> /private/var, /tmp).

    User-controlled intermediate directory symlinks (``.../contracts`` -> elsewhere)
    always have more than two path parts and are rejected.
    """
    # Top-level only: "/", "var" -> parts ('/', 'var') length 2
    if len(component.parts) > 2:
        return False
    try:
        final_abs.resolve().relative_to(component.resolve())
        return True
    except (OSError, ValueError):
        return False


def _assert_no_symlink_components(path: pathlib.Path) -> pathlib.Path:
    """Reject symlink leaf or user-controlled symlink parents; return path to open."""
    import stat as statmod

    p = path.expanduser()
    if not p.is_absolute():
        # Relative: walk from cwd through each user-supplied component (strict).
        accum = pathlib.Path.cwd()
        for part in p.parts:
            accum = accum / part
            try:
                st = accum.lstat()
            except OSError as exc:
                raise _contract_error(
                    "cannot stat contract path component: {}".format(exc),
                    {"path": str(accum)},
                ) from exc
            if statmod.S_ISLNK(st.st_mode):
                raise _contract_error(
                    "contract path must not contain symlink components",
                    {"path": str(accum)},
                )
        if not statmod.S_ISREG(accum.lstat().st_mode):
            raise _contract_error("contract path must be a regular file", {"path": str(accum)})
        return accum

    # Absolute: walk each component; allow only top-level OS path aliases.
    p = p.absolute()
    parts = p.parts
    if not parts:
        raise _contract_error("contract path is empty", {"path": str(path)})
    accum = pathlib.Path(parts[0])
    for part in parts[1:]:
        accum = accum / part
        try:
            st = accum.lstat()
        except OSError as exc:
            raise _contract_error(
                "cannot stat contract path component: {}".format(exc),
                {"path": str(accum)},
            ) from exc
        if statmod.S_ISLNK(st.st_mode):
            # Leaf symlink always rejected; intermediate only if not OS top-level alias.
            if accum == p or not _is_top_level_os_alias_symlink(accum, p):
                raise _contract_error(
                    "contract path must not contain symlink components",
                    {"path": str(accum)},
                )
    try:
        st = p.lstat()
    except OSError as exc:
        raise _contract_error("cannot stat contract file: {}".format(exc), {"path": str(p)}) from exc
    if statmod.S_ISLNK(st.st_mode):
        raise _contract_error("contract path must not be a symlink", {"path": str(p)})
    if not statmod.S_ISREG(st.st_mode):
        raise _contract_error("contract path must be a regular file", {"path": str(p)})
    return p


def load_contract_file(path: pathlib.Path) -> dict:
    """Load and parse a contract file. Rejects non-regular files and symlink escapes."""
    p = pathlib.Path(path)
    if not p.exists():
        raise _contract_error("contract file does not exist", {"path": str(p)})
    # Reject symlink leaf or any symlinked parent directory (fail closed).
    safe = _assert_no_symlink_components(p)
    try:
        text = safe.read_text(encoding="utf-8")
    except UnicodeError as exc:
        # Malformed contracts must classify as implementation-contract-invalid
        # (not top-level unexpected cli-failure).
        raise _contract_error(
            "contract file is not valid UTF-8: {}".format(exc),
            {"path": str(safe)},
        ) from exc
    except OSError as exc:
        raise _contract_error("cannot read contract file: {}".format(exc), {"path": str(safe)}) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _contract_error("contract JSON is invalid: {}".format(exc), {"path": str(safe)}) from exc
    if not isinstance(data, dict):
        raise _contract_error("contract root must be a JSON object", {"path": str(safe)})
    return validate_contract(data)


def validate_contract(data: dict) -> dict:
    """Validate contract schemaVersion 1 document; return normalized dict."""
    if not isinstance(data, dict):
        raise _contract_error("contract must be an object")
    if data.get("schemaVersion") != 1:
        raise _contract_error(
            "contract schemaVersion must be 1",
            {"schemaVersion": data.get("schemaVersion")},
        )
    task_id = data.get("taskId")
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise _contract_error("invalid taskId", {"taskId": task_id})
    target = data.get("target")
    if not isinstance(target, str) or not target.strip():
        raise _contract_error("target must be a non-empty string")
    try:
        target_norm = normalize_repo_relative(target) if target.strip() not in (".", "./") else "."
    except GrokWrapperError:
        if target.strip() in (".", "./"):
            target_norm = "."
        else:
            raise
    if target.strip() in (".", "./"):
        target_norm = "."

    write_scopes = data.get("writeScopes")
    if not isinstance(write_scopes, list) or not write_scopes:
        raise _contract_error("writeScopes must be a non-empty array when contract is present")
    scopes_out: List[dict] = []
    for i, scope in enumerate(write_scopes):
        if not isinstance(scope, dict):
            raise _contract_error("writeScopes[{}] must be an object".format(i))
        kind = scope.get("kind")
        if kind not in ("file", "subtree"):
            raise _contract_error(
                "writeScopes[{}].kind must be file or subtree".format(i),
                {"kind": kind},
            )
        sp = scope.get("path")
        if not isinstance(sp, str):
            raise _contract_error("writeScopes[{}].path must be a string".format(i))
        scopes_out.append({"kind": kind, "path": normalize_repo_relative(sp)})

    # Absent/null => no validations. Present non-array (incl. "" / false) => fail closed.
    if "requiredValidation" not in data or data.get("requiredValidation") is None:
        required: list = []
    else:
        required = data.get("requiredValidation")
        if not isinstance(required, list):
            raise _contract_error(
                "requiredValidation must be an array when present",
                {"type": type(required).__name__},
            )
    req_out: List[dict] = []
    for i, entry in enumerate(required):
        if not isinstance(entry, dict):
            raise _contract_error("requiredValidation[{}] must be an object".format(i))
        argv = entry.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise _contract_error(
                "requiredValidation[{}].argv must be a non-empty string array".format(i)
            )
        # subprocess.run rejects embedded NUL with ValueError; fail closed at contract load.
        for j, token in enumerate(argv):
            if "\x00" in token:
                raise _contract_error(
                    "requiredValidation[{}].argv[{}] must not contain NUL bytes".format(i, j),
                    {"index": j},
                )
        cwd = entry.get("cwd", ".")
        if not isinstance(cwd, str):
            raise _contract_error("requiredValidation[{}].cwd must be a string".format(i))
        if "\x00" in cwd:
            raise _contract_error("requiredValidation[{}].cwd must not contain NUL bytes".format(i))
        if cwd.strip() in (".", "./", ""):
            cwd_norm = "."
        else:
            cwd_norm = normalize_repo_relative(cwd)
        purpose = entry.get("purpose") or ""
        if purpose is not None and not isinstance(purpose, str):
            raise _contract_error("requiredValidation[{}].purpose must be a string".format(i))
        if isinstance(purpose, str) and "\x00" in purpose:
            raise _contract_error(
                "requiredValidation[{}].purpose must not contain NUL bytes".format(i)
            )
        req_out.append({"argv": list(argv), "cwd": cwd_norm, "purpose": purpose or ""})

    if "acceptanceCriteria" not in data or data.get("acceptanceCriteria") is None:
        acceptance: list = []
    else:
        acceptance = data.get("acceptanceCriteria")
        if not isinstance(acceptance, list):
            raise _contract_error(
                "acceptanceCriteria must be an array when present",
                {"type": type(acceptance).__name__},
            )

    objective = data.get("objective")
    if objective is not None and not isinstance(objective, str):
        raise _contract_error("objective must be a string when present")

    return {
        "schemaVersion": 1,
        "taskId": task_id,
        "objective": objective if isinstance(objective, str) else "",
        "target": target_norm,
        "writeScopes": scopes_out,
        "acceptanceCriteria": list(acceptance) if isinstance(acceptance, list) else [],
        "requiredValidation": req_out,
        "trustModel": _TRUST_MODEL,
    }


def assert_target_matches(contract: dict, cli_target_relative: str) -> None:
    """CLI target must match contract target after normalization."""
    cli = cli_target_relative.strip() if cli_target_relative else "."
    if cli in (".", "", "./"):
        cli_n = "."
    else:
        cli_n = normalize_repo_relative(cli)
    if contract.get("target") != cli_n:
        raise _contract_error(
            "contract target does not match CLI --target",
            {"contractTarget": contract.get("target"), "cliTarget": cli_n},
        )
