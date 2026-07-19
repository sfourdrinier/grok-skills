# wrapper/scripts/groklib/peer_doc.py
#
# Single peer.json read-modify-write spine under run_lock. All peer channel
# writers re-read, field-patch, and preserve unrelated keys. Lifecycle claim /
# terminal transitions stay owned by modes.peer_stop; lease/death writers use
# guarded patch helpers so concurrent stop ownership cannot be clobbered.

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

from groklib import GrokWrapperError, runstate

# Terminal peer channel outcomes (stop ownership cleared on these).
TERMINAL_PEER_LIFECYCLES = frozenset({"stopped", "failed"})
# Stop ownership / terminal must win over opportunistic death marking.
_STOP_OWNED_OR_TERMINAL = frozenset({"stopping", "stopped", "failed"})
# Death may only record from live-ish non-owned states.
_DEATH_ALLOWED_FROM = frozenset({"running", None})


def peer_json_path(run_paths: "runstate.RunPaths"):
    return run_paths.run_dir / "peer.json"


def read_peer_doc_unlocked(run_paths: "runstate.RunPaths") -> dict:
    """Read peer.json without locking (caller holds run_lock or accepts races)."""
    path = peer_json_path(run_paths)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GrokWrapperError(
            "acp-failure",
            "peer.json unreadable: {}".format(exc),
            {"runId": run_paths.run_id},
        ) from exc
    if not isinstance(doc, dict):
        raise GrokWrapperError("acp-failure", "peer.json is not an object")
    return doc


def write_peer_doc_unlocked(run_paths: "runstate.RunPaths", doc: dict) -> None:
    """Atomic peer.json write; caller must already hold run_lock when concurrent."""
    runstate.write_json_atomic(peer_json_path(run_paths), doc)


def mutate_peer_doc(
    run_paths: "runstate.RunPaths",
    mutator: Callable[[dict], Optional[dict]],
) -> dict:
    """Under run_lock: re-read peer.json, apply mutator, write when not None.

    ``mutator`` receives a shallow copy of the on-disk document and returns either
    the full document to write or ``None`` to leave disk unchanged. Unrelated
    fields are preserved because writers start from the re-read copy.
    """
    with runstate.run_lock(run_paths):
        current = read_peer_doc_unlocked(run_paths)
        updated = mutator(dict(current))
        if updated is None:
            return current
        if not isinstance(updated, dict):
            raise GrokWrapperError(
                "acp-failure",
                "peer.json mutator must return a dict or None",
                {"runId": run_paths.run_id},
            )
        write_peer_doc_unlocked(run_paths, updated)
        return updated


def patch_peer_fields(
    run_paths: "runstate.RunPaths",
    fields: Dict[str, Any],
    *,
    pop: Optional[Iterable[str]] = None,
    only_if_lifecycle_in: Optional[Set[Any]] = None,
    skip_if_lifecycle_in: Optional[Set[Any]] = None,
) -> Tuple[dict, bool]:
    """Field-patch peer.json under lock; return ``(doc, applied)``.

    Never replaces the whole document from a stale in-memory snapshot. Optional
    lifecycle guards refuse the write without raising (caller decides).
    """
    applied = False
    pop_keys = tuple(pop or ())

    def _mutator(doc: dict) -> Optional[dict]:
        nonlocal applied
        life = doc.get("lifecycle")
        if only_if_lifecycle_in is not None and life not in only_if_lifecycle_in:
            return None
        if skip_if_lifecycle_in is not None and life in skip_if_lifecycle_in:
            return None
        patched = dict(doc)
        for key, value in fields.items():
            patched[key] = value
        for key in pop_keys:
            patched.pop(key, None)
        applied = True
        return patched

    doc = mutate_peer_doc(run_paths, _mutator)
    return doc, applied


def patch_lease_expires(
    run_paths: "runstate.RunPaths",
    lease_expires_at: float,
    *,
    prompts_handled: Optional[int] = None,
) -> dict:
    """Renew leaseExpiresAt; optionally raise promptsHandled (never lower it).

    Never touches lifecycle or stopOwner. A stale resident snapshot cannot
    downgrade concurrent promptsHandled written by another path.
    """

    def _mutator(doc: dict) -> dict:
        patched = dict(doc)
        patched["leaseExpiresAt"] = float(lease_expires_at)
        if prompts_handled is not None:
            proposed = int(prompts_handled)
            current = doc.get("promptsHandled")
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                patched["promptsHandled"] = max(int(current), proposed)
            else:
                patched["promptsHandled"] = proposed
        return patched

    return mutate_peer_doc(run_paths, _mutator)


def mark_peer_died_if_allowed(run_paths: "runstate.RunPaths") -> Tuple[dict, bool]:
    """Record lifecycle=died only when stop/terminal ownership does not already win.

    Returns ``(doc, applied)``. Stopping / stopped / failed are preserved.
    """
    return patch_peer_fields(
        run_paths,
        {"lifecycle": "died"},
        only_if_lifecycle_in=set(_DEATH_ALLOWED_FROM),
        skip_if_lifecycle_in=set(_STOP_OWNED_OR_TERMINAL),
    )
