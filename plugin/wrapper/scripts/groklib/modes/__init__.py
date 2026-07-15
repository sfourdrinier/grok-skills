# wrapper/scripts/groklib/modes/__init__.py
#
# Mode registry: maps each C8 subcommand name to its run(args) handler. Every
# handler takes the parsed argparse.Namespace and returns a validated C4
# envelope dict (never printing anything itself; the entrypoint is the sole
# stdout writer). Task 11 extends MODES further with code/verify.

import argparse
from typing import Callable, Dict

from groklib.modes import cleanup, code, preflight, reason, review, status, verify

MODES: Dict[str, Callable[[argparse.Namespace], dict]] = {
    "preflight": preflight.run,
    "review": review.run,
    "reason": reason.run,
    "code": code.run,
    "verify": verify.run,
    "status": status.run,
    "cleanup": cleanup.run,
}
