#!/usr/bin/env python3
# plugin/scripts/tests/fixtures/echo_task_wrapper.py
#
# Test-only stand-in for the hardened Grok wrapper (grok_agent.py) that echoes
# back the resolved --task-file content, so the companion's shell-injection-safe
# task-passing path (`--task-file -` -> staged temp file -> wrapper) can be
# asserted end-to-end: whatever bytes arrive on the companion's stdin must reach
# the wrapper's --task-file verbatim, never shell-evaluated. It reads the file
# named by --task-file and prints exactly one JSON envelope carrying its content.
# It also echoes the full received argv so a caller can assert that flag VALUES
# (e.g. --target) reach the wrapper as literal argv tokens, never shell-evaluated.

import json
import sys


def main() -> int:
    argv = sys.argv[1:]
    mode = argv[0] if argv else "?"
    task_echo = None
    if "--task-file" in argv:
        index = argv.index("--task-file")
        if index + 1 < len(argv):
            with open(argv[index + 1], "r", encoding="utf-8") as handle:
                task_echo = handle.read()
    print(
        json.dumps(
            {
                "schemaVersion": 1,
                "mode": mode,
                "status": "success",
                "taskEcho": task_echo,
                "argv": argv,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
