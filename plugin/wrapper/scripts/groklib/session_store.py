# wrapper/scripts/groklib/session_store.py
#
# Per-run archive of the private-home Grok session store
# (<home>/.grok/sessions) into the run directory, and seed of that archive
# into a fresh private home for continuation runs.
#
# The archive contains MODEL CONVERSATION CONTENT: operator task text in
# prompt_history.jsonl, plus per-session state under cwd-bucketed UUID
# dirs and session_search.sqlite at the sessions root. It inherits the
# run dir's 0700 confinement (dirs 0700, files 0600). Secret redaction
# does NOT apply here because the archive is never emitted to stdout -
# only stored under the run dir and removed with
# `cleanup --run-id --confirm` (whole run dir removal). See SECURITY.md
# "Session archives".

import datetime
import json
import os
import pathlib
import shutil
from typing import Optional

from groklib import log_stderr

_SCHEMA_VERSION = 1
_SESSION_DIR_NAME = "session"
_SESSIONS_NAME = "sessions"
_META_NAME = "session-meta.json"


def _log(function: str, message: str) -> None:
    log_stderr("session_store", function, message)


def _apply_private_modes(root: pathlib.Path) -> None:
    """Walk ``root`` and set dirs to 0700, files to 0600 (best-effort)."""
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        try:
            os.chmod(dirpath, 0o700)
        except OSError as exc:
            _log("_apply_private_modes", "chmod dir {}: {}".format(dirpath, exc))
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                os.chmod(fpath, 0o600)
            except OSError as exc:
                _log("_apply_private_modes", "chmod file {}: {}".format(fpath, exc))


def _write_meta_json(path: pathlib.Path, meta: dict) -> None:
    text = json.dumps(meta, indent=2, sort_keys=True) + "\n"
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


def archive_session(
    home_dir: pathlib.Path,
    run_dir: pathlib.Path,
    session_id: str,
) -> Optional[dict]:
    """Copy ``<home>/.grok/sessions`` into ``<run_dir>/session/sessions`` and write meta.

    Returns the meta dict on success, or None when no sessions store exists or
    any step fails (failures are logged; never raised).
    """
    try:
        src = pathlib.Path(home_dir) / ".grok" / _SESSIONS_NAME
        if not src.is_dir():
            _log(
                "archive_session",
                "no sessions store under {}; skipping archive".format(home_dir),
            )
            return None
        session_root = pathlib.Path(run_dir) / _SESSION_DIR_NAME
        dest = session_root / _SESSIONS_NAME
        session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(str(session_root), 0o700)
        except OSError:
            pass
        if dest.exists():
            _log(
                "archive_session",
                "archive target already exists at {}; refusing overwrite".format(dest),
            )
            return None
        shutil.copytree(str(src), str(dest))
        _apply_private_modes(session_root)
        meta = {
            "schemaVersion": _SCHEMA_VERSION,
            "grokSessionId": session_id,
            "archivedAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _write_meta_json(session_root / _META_NAME, meta)
        try:
            os.chmod(str(session_root / _META_NAME), 0o600)
        except OSError:
            pass
        return meta
    except Exception as exc:
        _log("archive_session", "archive failed: {}".format(exc))
        return None


def load_session_meta(run_dir: pathlib.Path) -> Optional[dict]:
    """Load ``<run_dir>/session/session-meta.json``; None when missing or invalid."""
    path = pathlib.Path(run_dir) / _SESSION_DIR_NAME / _META_NAME
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        _log("load_session_meta", "could not read {}: {}".format(path, exc))
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def seed_sessions(run_dir: pathlib.Path, home_dir: pathlib.Path) -> bool:
    """Copy archived sessions into ``<home>/.grok/sessions`` for a fresh private home.

    Returns True on success, False when no archive exists or copy fails.
    """
    try:
        src = pathlib.Path(run_dir) / _SESSION_DIR_NAME / _SESSIONS_NAME
        if not src.is_dir():
            _log(
                "seed_sessions",
                "no session archive under {}; cannot seed".format(run_dir),
            )
            return False
        grok_dir = pathlib.Path(home_dir) / ".grok"
        dest = grok_dir / _SESSIONS_NAME
        grok_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(str(grok_dir), 0o700)
        except OSError:
            pass
        if dest.exists():
            _log(
                "seed_sessions",
                "destination sessions already exist at {}; refusing overwrite".format(dest),
            )
            return False
        shutil.copytree(str(src), str(dest))
        _apply_private_modes(dest)
        return True
    except Exception as exc:
        _log("seed_sessions", "seed failed: {}".format(exc))
        return False
