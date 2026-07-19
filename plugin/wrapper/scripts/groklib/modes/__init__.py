# wrapper/scripts/groklib/modes/__init__.py
#
# Mode registry: maps each C8 subcommand name to its run(args) handler. Every
# handler takes the parsed argparse.Namespace and returns a validated C4
# envelope dict (never printing anything itself; the entrypoint is the sole
# stdout writer). Hardened-direct is NOT a subcommand: it is reached via
# `code --integration direct` inside modes.code.run. The bare wrapper defaults
# `--integration` to worktree (fail-closed); the product companion passes
# direct only after per-repo consent.

import argparse
from typing import Callable, Dict

from groklib.modes import cleanup, code, handoff, peer, preflight, reason, review, status, verify

MODES: Dict[str, Callable[[argparse.Namespace], dict]] = {
    "preflight": preflight.run,
    "review": review.run,
    "reason": reason.run,
    "code": code.run,
    "verify": verify.run,
    "status": status.run,
    "cleanup": cleanup.run,
    "handoff": handoff.run,
    "peer-start": peer.run_peer_start,
    "peer-prompt": peer.run_peer_prompt,
    "peer-stop": peer.run_peer_stop,
}
