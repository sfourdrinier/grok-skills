#!/usr/bin/env python3
# plugin/scripts/tests/fixtures/cancel-tree.py
#
# Harmless cancel fixture: parent prints "parentPid childPid", installs SIGTERM
# to kill the child (mirrors grok_agent._install_sigterm_handler), then waits.
# Child ignores SIGTERM so a missed parent handler leaves it alive.

from __future__ import annotations

import os
import signal
import sys
import time


def main() -> None:
    child_pid = os.fork()
    if child_pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)

    def handle_term(_signum: int, _frame: object) -> None:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_term)
    sys.stdout.write(f"{os.getpid()} {child_pid}\n")
    sys.stdout.flush()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
