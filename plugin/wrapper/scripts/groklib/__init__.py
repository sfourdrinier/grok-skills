# wrapper/scripts/groklib/__init__.py

import os
from typing import Dict, Optional


class GrokWrapperError(Exception):
    """Base exception for every classified failure raised by groklib.

    Every raise site attaches an ``error_class`` matching one of the exact
    C4 envelope ``error.class`` strings, plus an optional ``detail`` object
    with structured, non-secret context. Callers building the C4 result
    envelope read both fields directly off the caught exception.
    """

    def __init__(self, error_class: str, message: str, detail: Optional[Dict[str, object]] = None) -> None:
        super().__init__(message)
        self.error_class: str = error_class
        self.detail: Dict[str, object] = detail if detail is not None else {}


def log_stderr(component: str, function: str, message: str) -> None:
    """Write one diagnostic line to stderr: ``[groklib.<component>] <function>: <message>``.

    Uses ``os.write(2, ...)`` directly (no ``sys`` import) so callers with
    import-isolation constraints (see ``groklib/runstate.py``) can log
    without pulling in extra stdlib surface area. This is the single
    ``os.write(2, ...)`` call site for the entire package; every module
    (``progress.py``, ``runstate.py``, ...) delegates here through a thin
    module-local wrapper that pre-binds its own ``component`` prefix,
    instead of duplicating the implementation.
    """
    os.write(2, "[groklib.{}] {}: {}\n".format(component, function, message).encode("utf-8"))
