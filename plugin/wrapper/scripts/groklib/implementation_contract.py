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

# Display-field size caps (Phase 1 finding 4). Enforced at contract load and
# mirrored on handoff contractSummary so a tampered manifest cannot push
# multi-MB objective/criteria through the handoff response.
OBJECTIVE_MAX_CHARS = 2000
ACCEPTANCE_CRITERIA_MAX_ITEMS = 32
ACCEPTANCE_CRITERION_MAX_CHARS = 500


def trust_model() -> str:
    return _TRUST_MODEL


def _contract_error(message: str, detail: Optional[dict] = None) -> GrokWrapperError:
    return GrokWrapperError("implementation-contract-invalid", message, detail or {})


def objective_criteria_bound_errors(
    objective: object,
    acceptance: object,
    *,
    field_prefix: str = "",
) -> List[str]:
    """Return bound-violation messages for objective / acceptanceCriteria.

    Shared by ``validate_contract`` (raises) and handoff manifest validation
    (returns error list). ``field_prefix`` is prepended to each field name
    (e.g. ``\"contractSummary.\"``) so callers can label nested paths.
    """
    errors: List[str] = []
    obj_label = "{}objective".format(field_prefix)
    ac_label = "{}acceptanceCriteria".format(field_prefix)

    if isinstance(objective, str) and len(objective) > OBJECTIVE_MAX_CHARS:
        errors.append(
            "{} exceeds {} characters (got {})".format(
                obj_label, OBJECTIVE_MAX_CHARS, len(objective)
            )
        )

    if not isinstance(acceptance, list):
        # Caller is responsible for type checks when the field is present;
        # only enforce item/count bounds when we already have a list.
        return errors

    if len(acceptance) > ACCEPTANCE_CRITERIA_MAX_ITEMS:
        errors.append(
            "{} exceeds {} items (got {})".format(
                ac_label, ACCEPTANCE_CRITERIA_MAX_ITEMS, len(acceptance)
            )
        )

    for i, item in enumerate(acceptance):
        if not isinstance(item, str):
            errors.append("{}[{}] must be a string".format(ac_label, i))
            continue
        stripped = item.strip()
        if len(stripped) > ACCEPTANCE_CRITERION_MAX_CHARS:
            errors.append(
                "{}[{}] exceeds {} characters after strip (got {})".format(
                    ac_label, i, ACCEPTANCE_CRITERION_MAX_CHARS, len(stripped)
                )
            )
    return errors


def normalize_repo_relative(path: str) -> str:
    """Normalize an operator-supplied repo-relative path (contract scopes/target).

    Treats backslash as a path separator (Windows-style operator input). Do **not**
    use this for Git-reported paths - on POSIX backslash is a valid filename char.
    Rejects Windows drive-letter prefixes (``C:...``) because operators may paste
    absolute Windows paths into contracts.
    """
    if path is None or not isinstance(path, str):
        raise _contract_error("path must be a non-empty string", {"path": path})
    raw = path.strip().replace("\\", "/")
    return _normalize_repo_relative_raw(
        raw, original=path, reject_windows_drive=True
    )


def normalize_git_repo_path(path: str) -> str:
    """Normalize a Git-reported repo-relative path for scope checks.

    Only forward slash is a separator. Backslash is preserved as a literal
    character so a root file named ``pkg\\evil.ts`` is not treated as under ``pkg/``.
    Does **not** strip whitespace: leading/trailing spaces are valid filename chars.
    Does **not** reject a second-character colon (``a:b.txt`` is a legal Git path
    on POSIX); only true absolute paths (leading ``/`` / ``os.path.isabs``) fail.
    """
    if path is None or not isinstance(path, str):
        raise _contract_error("path must be a non-empty string", {"path": path})
    # Never strip: Git paths may begin/end with spaces and still be distinct names.
    return _normalize_repo_relative_raw(
        path, original=path, reject_windows_drive=False
    )


def _normalize_repo_relative_raw(
    raw: str, *, original: str, reject_windows_drive: bool = True
) -> str:
    if not raw or "\x00" in raw:
        raise _contract_error("path is empty or contains NUL", {"path": original})
    if raw in (".", "./"):
        return "."
    # Absolute path: leading slash or OS absolute. Operator paths also reject
    # Windows drive-letter forms (``C:Users/...``); Git-reported paths do not,
    # because ``a:b.txt`` is a valid single-component filename under Git/POSIX.
    if os.path.isabs(raw) or raw.startswith("/"):
        raise _contract_error("path must be repository-relative (not absolute)", {"path": original})
    cleaned: List[str] = []
    # Split only on forward slash (never on backslash).
    for p in raw.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise _contract_error("path must not contain '..'", {"path": original})
        # Operator paths: reject drive-letter forms on every component so
        # ``./C:/Users/...`` cannot bypass a leading-raw ``C:`` check.
        if reject_windows_drive and len(p) > 1 and p[1] == ":" and p[0].isalpha():
            raise _contract_error(
                "path must be repository-relative (not absolute)", {"path": original}
            )
        cleaned.append(p)
    if not cleaned:
        raise _contract_error("path resolves empty", {"path": original})
    # Also reject bare ``C:foo`` style when it never hit a slash split with a drive.
    if reject_windows_drive and len(raw) > 1 and raw[1] == ":" and raw[0].isalpha():
        raise _contract_error("path must be repository-relative (not absolute)", {"path": original})
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


def load_optional_contract_arg(contract_file: object) -> Optional[dict]:
    """Load ``--contract-file`` when present; blank/whitespace is invalid (not absent).

    Shared by code/direct/peer-start so present-but-empty forms
    (``--contract-file`` / ``--contract-file=`` / empty shell expansion) always
    fail closed as ``implementation-contract-invalid`` rather than silently
    running without writeScopes / requiredValidation.
    """
    if contract_file is None:
        return None
    if not str(contract_file).strip():
        raise _contract_error(
            "--contract-file was provided but is empty; omit the flag or pass a path",
            {"contractFile": contract_file},
        )
    return load_contract_file(pathlib.Path(str(contract_file).strip()))


def load_contract_file(path: pathlib.Path) -> dict:
    """Load and parse a contract file. Rejects non-regular files and symlink escapes."""
    p = pathlib.Path(path)
    if not p.exists():
        raise _contract_error("contract file does not exist", {"path": str(p)})
    # Reject symlink leaf or any symlinked parent directory (fail closed).
    safe = _assert_no_symlink_components(p)
    # Open with O_NOFOLLOW so a TOCTOU symlink swap after lstat cannot pivot.
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(safe), flags)
    except OSError as exc:
        raise _contract_error(
            "cannot open contract file: {}".format(exc),
            {"path": str(safe)},
        ) from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            text = fh.read()
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
    """Validate contract schemaVersion 1 document; return normalized dict.

    Collects **all** field violations then raises once with
    ``error.detail.violations`` so orchestrators fix a contract in one round-trip
    (GitHub issue #8) instead of one error per launch.
    """
    if not isinstance(data, dict):
        raise _contract_error("contract must be an object")

    violations: List[str] = []

    if data.get("schemaVersion") != 1:
        violations.append(
            "contract schemaVersion must be 1 (got {!r})".format(data.get("schemaVersion"))
        )

    task_id = data.get("taskId")
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        violations.append("invalid taskId (must match {!r})".format(_TASK_ID_RE.pattern))

    target = data.get("target")
    target_norm = "."
    if not isinstance(target, str) or not target.strip():
        violations.append("target must be a non-empty string")
    else:
        try:
            if target.strip() in (".", "./"):
                target_norm = "."
            else:
                target_norm = normalize_repo_relative(target)
        except GrokWrapperError as exc:
            violations.append("target path invalid: {}".format(exc))

    write_scopes = data.get("writeScopes")
    scopes_out: List[dict] = []
    if not isinstance(write_scopes, list) or not write_scopes:
        violations.append(
            "writeScopes must be a non-empty array of {kind: file|subtree, path} objects"
        )
    else:
        for i, scope in enumerate(write_scopes):
            if not isinstance(scope, dict):
                violations.append(
                    "writeScopes[{}] must be an object {{kind, path}} (not a string)".format(i)
                )
                continue
            kind = scope.get("kind")
            if kind not in ("file", "subtree"):
                violations.append(
                    "writeScopes[{}].kind must be file or subtree (got {!r})".format(i, kind)
                )
            sp = scope.get("path")
            if not isinstance(sp, str):
                violations.append("writeScopes[{}].path must be a string".format(i))
                continue
            try:
                path_norm = normalize_repo_relative(sp)
            except GrokWrapperError as exc:
                violations.append("writeScopes[{}].path invalid: {}".format(i, exc))
                continue
            if kind in ("file", "subtree"):
                scopes_out.append({"kind": kind, "path": path_norm})

    # Absent/null => no validations. Present non-array (incl. "" / false) => fail closed.
    if "requiredValidation" not in data or data.get("requiredValidation") is None:
        required: list = []
    else:
        required = data.get("requiredValidation")
        if not isinstance(required, list):
            violations.append(
                "requiredValidation must be an array when present (got {})".format(
                    type(required).__name__
                )
            )
            required = []
    req_out: List[dict] = []
    for i, entry in enumerate(required):
        if not isinstance(entry, dict):
            violations.append("requiredValidation[{}] must be an object".format(i))
            continue
        argv = entry.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            violations.append(
                "requiredValidation[{}].argv must be a non-empty string array".format(i)
            )
            continue
        bad_nul = False
        for j, token in enumerate(argv):
            if "\x00" in token:
                violations.append(
                    "requiredValidation[{}].argv[{}] must not contain NUL bytes".format(i, j)
                )
                bad_nul = True
        if bad_nul:
            continue
        cwd = entry.get("cwd", ".")
        if not isinstance(cwd, str):
            violations.append("requiredValidation[{}].cwd must be a string".format(i))
            continue
        if "\x00" in cwd:
            violations.append("requiredValidation[{}].cwd must not contain NUL bytes".format(i))
            continue
        if cwd.strip() in (".", "./", ""):
            cwd_norm = "."
        else:
            try:
                cwd_norm = normalize_repo_relative(cwd)
            except GrokWrapperError as exc:
                violations.append("requiredValidation[{}].cwd invalid: {}".format(i, exc))
                continue
        purpose = entry.get("purpose") or ""
        if purpose is not None and not isinstance(purpose, str):
            violations.append("requiredValidation[{}].purpose must be a string".format(i))
            continue
        if isinstance(purpose, str) and "\x00" in purpose:
            violations.append(
                "requiredValidation[{}].purpose must not contain NUL bytes".format(i)
            )
            continue
        # Optional monorepo skip: run only when any changed path matches a prefix
        # (issue #8 onlyIfChanged). Absent/null => always run.
        only_if: List[str] = []
        if "onlyIfChanged" in entry and entry.get("onlyIfChanged") is not None:
            raw_only = entry.get("onlyIfChanged")
            if not isinstance(raw_only, list) or not raw_only:
                violations.append(
                    "requiredValidation[{}].onlyIfChanged must be a non-empty "
                    "array of path-prefix strings when present".format(i)
                )
            else:
                for k, pref in enumerate(raw_only):
                    if not isinstance(pref, str) or not pref.strip() or "\x00" in pref:
                        violations.append(
                            "requiredValidation[{}].onlyIfChanged[{}] must be a "
                            "non-empty string without NUL".format(i, k)
                        )
                        continue
                    try:
                        only_if.append(normalize_repo_relative(pref.strip()))
                    except GrokWrapperError as exc:
                        violations.append(
                            "requiredValidation[{}].onlyIfChanged[{}] invalid: {}".format(
                                i, k, exc
                            )
                        )
        entry_out = {"argv": list(argv), "cwd": cwd_norm, "purpose": purpose or ""}
        if only_if:
            entry_out["onlyIfChanged"] = only_if
        req_out.append(entry_out)

    if "acceptanceCriteria" not in data or data.get("acceptanceCriteria") is None:
        acceptance: list = []
    else:
        acceptance = data.get("acceptanceCriteria")
        if not isinstance(acceptance, list):
            violations.append(
                "acceptanceCriteria must be an array when present (got {})".format(
                    type(acceptance).__name__
                )
            )
            acceptance = []

    objective = data.get("objective")
    if objective is not None and not isinstance(objective, str):
        violations.append("objective must be a string when present")

    # Size caps: fail closed BEFORE Grok spawns (Phase 1 finding 4).
    ac_value = list(acceptance) if isinstance(acceptance, list) else []
    obj_value = objective if isinstance(objective, str) else None
    bound_errs = objective_criteria_bound_errors(obj_value, ac_value)
    violations.extend(bound_errs)

    if violations:
        raise _contract_error(
            "; ".join(violations[:5])
            + ("; ... ({} more)".format(len(violations) - 5) if len(violations) > 5 else ""),
            {"violations": violations, "violationCount": len(violations)},
        )

    # Re-validate task_id for type checkers after violation gate (always valid here).
    assert isinstance(task_id, str)

    return {
        "schemaVersion": 1,
        "taskId": task_id,
        "objective": objective if isinstance(objective, str) else "",
        "target": target_norm,
        "writeScopes": scopes_out,
        "acceptanceCriteria": ac_value,
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


def validation_matches_changed(entry: dict, changed_paths) -> bool:
    """True when a requiredValidation entry should run for the given changed paths.

    Absent/empty ``onlyIfChanged`` => always run. When set, run if any changed
    path equals a prefix or is under ``prefix/`` (issue #8 monorepo scoping).
    Prefix ``.`` / ``./`` means "any change" (repo root wildcard).
    """
    prefixes = entry.get("onlyIfChanged") if isinstance(entry, dict) else None
    if not prefixes:
        return True
    changed = list(changed_paths or [])
    if not changed:
        return False
    for pref in prefixes:
        if not isinstance(pref, str) or not pref:
            continue
        pref_n = pref.strip().rstrip("/")
        # Repo-root wildcard: any non-empty change set matches.
        if pref_n in (".", "", "./"):
            return True
        for path in changed:
            if not isinstance(path, str):
                continue
            if path == pref_n or path.startswith(pref_n + "/"):
                return True
    return False
