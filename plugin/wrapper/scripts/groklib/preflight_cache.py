# wrapper/scripts/groklib/preflight_cache.py
#
# Version-keyed short-lived preflight cache (Wave 1 C-A6). A positive readiness
# result is stored under the state root so live modes can skip a full re-probe
# for a few minutes. Fail-closed: missing, stale, malformed, ok=false, or
# version-mismatched cache always means re-run readiness. Never assumes ready.

import json
import os
import pathlib
import time
from typing import Dict, Optional

from groklib import GrokWrapperError, log_stderr, runstate

CACHE_FILENAME = "preflight-cache.json"
# Short TTL so a daily Grok CLI release is not trusted forever; keyed by version.
DEFAULT_TTL_MS = 15 * 60 * 1000


def _log(function: str, message: str) -> None:
    log_stderr("preflight_cache", function, message)


def cache_path() -> pathlib.Path:
    return runstate.state_root() / CACHE_FILENAME


def _now_ms() -> int:
    return int(time.time() * 1000)


def load_cache() -> Optional[Dict[str, object]]:
    """Return the cache dict if it is well-formed, else None (fail closed)."""
    path = cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log("load_cache", "malformed preflight cache JSON: {}".format(exc))
        return None
    if not isinstance(data, dict):
        _log("load_cache", "preflight cache is not a JSON object")
        return None
    version = data.get("version")
    checked = data.get("checkedAtMs")
    ok = data.get("ok")
    if not isinstance(version, str) or not version.strip():
        return None
    if not isinstance(checked, int) or isinstance(checked, bool):
        return None
    if not isinstance(ok, bool):
        return None
    identity = data.get("executableIdentity")
    if identity is not None and not isinstance(identity, str):
        return None
    return {
        "version": version,
        "checkedAtMs": checked,
        "ok": ok,
        "executableIdentity": identity,
    }


def executable_identity(binary: pathlib.Path) -> str:
    """Canonical executable identity: resolved path + mtime ns + size."""
    path = pathlib.Path(binary)
    resolved = path.resolve()
    st = resolved.stat()
    return "{}:{}:{}".format(resolved, st.st_mtime_ns, st.st_size)


def is_valid(
    version: str,
    *,
    now_ms: Optional[int] = None,
    ttl_ms: int = DEFAULT_TTL_MS,
    executable_identity: Optional[str] = None,
) -> bool:
    """True only when cache exists, ok=true, version matches, and within TTL."""
    if not isinstance(version, str) or not version.strip():
        return False
    data = load_cache()
    if data is None:
        return False
    if data["ok"] is not True:
        return False
    if data["version"] != version:
        return False
    if executable_identity:
        cached_id = data.get("executableIdentity")
        if cached_id != executable_identity:
            return False
    clock = _now_ms() if now_ms is None else now_ms
    age = clock - int(data["checkedAtMs"])
    if age < 0 or age > ttl_ms:
        return False
    return True


def write_ok(
    version: str,
    *,
    checked_at_ms: Optional[int] = None,
    executable_identity: Optional[str] = None,
) -> None:
    """Persist a positive preflight result for ``version`` (best-effort)."""
    if not isinstance(version, str) or not version.strip():
        raise GrokWrapperError(
            "validation-failure",
            "preflight cache requires a non-empty version string",
            {"version": version},
        )
    payload = {
        "version": version,
        "checkedAtMs": _now_ms() if checked_at_ms is None else int(checked_at_ms),
        "ok": True,
    }
    if executable_identity:
        payload["executableIdentity"] = executable_identity
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp + rename; prefer O_NOFOLLOW when available.
        tmp = path.with_suffix(path.suffix + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(tmp), flags, 0o600)
        except OSError:
            # O_NOFOLLOW may fail on some platforms for non-symlinks; retry without.
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        # Cache write failure must never fail a successful preflight; log only.
        _log("write_ok", "could not write preflight cache {}: {}".format(path, exc))


def invalidate() -> None:
    """Best-effort delete of the cache file."""
    path = cache_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log("invalidate", "could not remove preflight cache {}: {}".format(path, exc))


def _auth_present() -> "tuple[bool, list]":
    from groklib.modes._shared import AUTH_FILE_NAMES, source_grok_dir

    grok_dir = source_grok_dir()
    missing = [name for name in AUTH_FILE_NAMES if not (grok_dir / name).is_file()]
    return (not missing, missing)


def ensure_ready(binary: pathlib.Path, *, force: bool = False) -> str:
    """Return the resolved Grok version if readiness is cached or re-verified.

    On cache miss (or ``force``), re-checks runnable Grok version + auth material.
    On cache hit, STILL re-checks auth presence (cheap; prevents stale green after
    auth deletion). Heavier probes stay on explicit ``preflight`` / setup.
    """
    from groklib import grokcli

    identity = None
    try:
        identity = executable_identity(binary)
    except OSError:
        identity = None
    cached = load_cache()
    cached_version = ""
    if cached and isinstance(cached.get("version"), str):
        cached_version = str(cached["version"])
    auth_ok, missing = _auth_present()
    if not auth_ok:
        invalidate()
        raise GrokWrapperError(
            "auth-missing",
            "missing authentication file(s): {}. Run /grok:setup or /grok:preflight.".format(
                ", ".join(missing)
            ),
            {"missingAuthFileNames": missing},
        )
    if (
        not force
        and identity
        and cached_version
        and is_valid(cached_version, executable_identity=identity)
    ):
        _log(
            "ensure_ready",
            "preflight cache hit for identity {}".format(identity or cached_version),
        )
        return cached_version
    version = grokcli.check_version(binary)

    write_ok(version, executable_identity=identity)
    _log("ensure_ready", "preflight cache refreshed for version {}".format(version))
    return version
