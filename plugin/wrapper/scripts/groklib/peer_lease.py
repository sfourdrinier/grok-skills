# wrapper/scripts/groklib/peer_lease.py
#
# Peer-channel private-home lease (Task 5.3 amendment 2). MAX_PEER_LEASE is
# separate from MAX_RUN_TIMEOUT; each peer-prompt renews the lease. The stale-
# home reaper consults this module so a live ACP child is never reaped.

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Optional

from groklib import log_stderr
from groklib import platformsupport

PEER_LEASE_FILENAME = "peer.lease"
DEFAULT_MAX_PEER_LEASE_SECONDS = 2 * 3600
_FILE_MODE = 0o600


def _log(function: str, message: str) -> None:
    log_stderr("peer_lease", function, message)


def write_peer_lease(
    directory: pathlib.Path,
    *,
    child_pid: int,
    child_start_token: Optional[str],
    lease_seconds: int = DEFAULT_MAX_PEER_LEASE_SECONDS,
) -> None:
    """Write/renew ``directory/peer.lease`` (0600) binding the ACP child + expiry."""
    expires = time.time() + max(1, int(lease_seconds))
    payload = {
        "schemaVersion": 1,
        "childPid": int(child_pid),
        "childStartToken": child_start_token,
        "leaseExpiresAt": expires,
        "maxPeerLeaseSeconds": int(lease_seconds),
    }
    path = directory / PEER_LEASE_FILENAME
    try:
        # Local 0600 write (avoid importing runstate - circular with reaper).
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        platformsupport.restrict_file_permissions(path)
    except OSError as exc:
        _log("write_peer_lease", "could not write peer lease for {}: {}".format(directory, exc))


def peer_lease_keeps_home_alive(candidate: pathlib.Path) -> bool:
    """True when a peer.lease says the ACP child is still live with a fresh lease."""
    lease_path = candidate / PEER_LEASE_FILENAME
    if not lease_path.is_file():
        return False
    try:
        with open(lease_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    expires = payload.get("leaseExpiresAt")
    if not isinstance(expires, (int, float)) or time.time() > float(expires):
        return False
    child_pid = payload.get("childPid")
    if not isinstance(child_pid, int) or isinstance(child_pid, bool):
        return False
    if not platformsupport.process_is_alive(child_pid):
        return False
    stored_token = payload.get("childStartToken")
    current_token = platformsupport.process_start_token(child_pid)
    if isinstance(stored_token, str) and current_token is not None and stored_token != current_token:
        return False
    return True


def peer_lease_present(candidate: pathlib.Path) -> bool:
    return (candidate / PEER_LEASE_FILENAME).is_file()
