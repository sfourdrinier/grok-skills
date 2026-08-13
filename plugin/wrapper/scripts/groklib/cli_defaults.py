# wrapper/scripts/groklib/cli_defaults.py
#
# Product defaults for the installed Grok CLI child: default model id,
# reasoning-effort vocabulary, and the --no-plan pin. Data lives in
# plugin/references/grok-cli-defaults.json - never hardcode a second copy.

from __future__ import annotations

import json
import pathlib
from typing import List, Optional

from groklib import GrokWrapperError

_DATA_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "references"
    / "grok-cli-defaults.json"
)

_LOADED = False
_DOC: dict = {}
_BOOTSTRAP_NAMES = (
    "DEFAULT_MODEL",
    "REASONING_EFFORT_VALUES",
    "NO_PLAN_DEFAULT",
    "DEPRECATED_MODELS",
)


def cli_defaults_ssot_path() -> pathlib.Path:
    return _DATA_PATH


def reset_cli_defaults_cache() -> None:
    """Drop cached SSOT so the next attribute access reloads (tests)."""
    global _LOADED, _DOC
    _LOADED = False
    _DOC = {}
    g = globals()
    for name in _BOOTSTRAP_NAMES:
        g.pop(name, None)


def load_cli_defaults() -> dict:
    global _LOADED, _DOC
    if _LOADED:
        return _DOC
    path = cli_defaults_ssot_path()
    if not path.is_file():
        raise GrokWrapperError(
            "cli-failure",
            "cli-defaults SSOT missing at {}".format(path),
            {"path": str(path)},
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrokWrapperError(
            "cli-failure",
            "cli-defaults SSOT unreadable at {}: {}".format(path, exc),
            {"path": str(path)},
        ) from exc
    model = doc.get("defaultModel")
    efforts = doc.get("reasoningEffortValues")
    if not isinstance(model, str) or not model.strip():
        raise GrokWrapperError(
            "cli-failure",
            "cli-defaults SSOT missing defaultModel",
            {"path": str(path)},
        )
    if not isinstance(efforts, list) or not efforts or not all(
        isinstance(v, str) and v for v in efforts
    ):
        raise GrokWrapperError(
            "cli-failure",
            "cli-defaults SSOT has empty/invalid reasoningEffortValues",
            {"path": str(path)},
        )
    if not isinstance(doc.get("noPlanDefault"), bool):
        raise GrokWrapperError(
            "cli-failure",
            "cli-defaults SSOT noPlanDefault must be a boolean",
            {"path": str(path)},
        )
    _DOC = doc
    _LOADED = True
    return _DOC


def _doc() -> dict:
    return load_cli_defaults()


DEFAULT_MODEL: str
REASONING_EFFORT_VALUES: tuple
NO_PLAN_DEFAULT: bool
DEPRECATED_MODELS: tuple


def _bootstrap() -> None:
    global DEFAULT_MODEL, REASONING_EFFORT_VALUES, NO_PLAN_DEFAULT, DEPRECATED_MODELS
    doc = load_cli_defaults()
    DEFAULT_MODEL = str(doc["defaultModel"])
    REASONING_EFFORT_VALUES = tuple(str(v) for v in doc["reasoningEffortValues"])
    NO_PLAN_DEFAULT = bool(doc["noPlanDefault"])
    deprecated = doc.get("deprecatedModels") or []
    DEPRECATED_MODELS = tuple(str(v) for v in deprecated if isinstance(v, str))


def __getattr__(name: str):
    if name in _BOOTSTRAP_NAMES:
        _bootstrap()
        return globals()[name]
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))


def is_same_model_family(effective: str, requested_model: str) -> bool:
    """True iff ``effective`` is the requested model or a hyphen-delimited sub-variant.

    A raw ``startswith`` is wrong: requesting ``grok-4`` would then accept
    ``grok-4.5``. The boundary is a literal ``-`` separator.
    """
    return effective == requested_model or effective.startswith(requested_model + "-")


def parse_reasoning_effort(raw: object) -> str:
    """Return a canonical effort token or raise ValueError (fail closed)."""
    if not isinstance(raw, str):
        raise ValueError("reasoning effort must be a string")
    value = raw.strip().lower()
    if value not in REASONING_EFFORT_VALUES:
        raise ValueError(
            "reasoning effort must be one of {}; got {!r}".format(
                ", ".join(REASONING_EFFORT_VALUES), raw
            )
        )
    return value


def argparse_reasoning_effort(raw: str) -> str:
    """argparse type: invalid/blank effort is a usage error, never silently dropped."""
    import argparse

    try:
        return parse_reasoning_effort(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_requested_model(raw: object) -> str:
    """Non-empty model id, or ValueError. Omitted-flag default is DEFAULT_MODEL."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("model id must be a non-empty string")
    return raw.strip()


def argparse_requested_model(raw: str) -> str:
    import argparse

    try:
        return parse_requested_model(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def requested_model_from_args(args: object) -> str:
    raw = getattr(args, "model", None)
    if raw is None:
        return DEFAULT_MODEL
    if not isinstance(raw, str):
        return DEFAULT_MODEL
    try:
        return parse_requested_model(raw)
    except ValueError as exc:
        raise GrokWrapperError("usage-error", str(exc), {"model": raw}) from exc


def reasoning_effort_from_args(args: object) -> Optional[str]:
    raw = getattr(args, "reasoning_effort", None)
    if raw is None or not isinstance(raw, str):
        return None
    try:
        return parse_reasoning_effort(raw)
    except ValueError as exc:
        raise GrokWrapperError("usage-error", str(exc), {"reasoningEffort": raw}) from exc


def no_plan_from_args(args: object) -> bool:
    raw = getattr(args, "no_plan", NO_PLAN_DEFAULT)
    if isinstance(raw, bool):
        return raw
    return NO_PLAN_DEFAULT


def mode_run_cli_kwargs(args: object) -> dict:
    """Kwargs to spread onto ModeRun / run_*_mode so effort/no-plan stay one source."""
    return {
        "reasoning_effort": reasoning_effort_from_args(args),
        "no_plan": no_plan_from_args(args),
    }


def require_reasoning_effort(raw: Optional[str]) -> Optional[str]:
    """Validate an optional effort for argv builders; None stays omitted."""
    if raw is None:
        return None
    try:
        return parse_reasoning_effort(raw)
    except ValueError as exc:
        raise GrokWrapperError("usage-error", str(exc), {"reasoningEffort": raw}) from exc


def append_child_pins(
    argv: List[str],
    *,
    reasoning_effort: Optional[str] = None,
    no_plan: Optional[bool] = None,
) -> None:
    """Append the shared --reasoning-effort / --no-plan pins (C6 globals)."""
    effort = require_reasoning_effort(reasoning_effort)
    if effort is not None:
        argv.extend(["--reasoning-effort", effort])
    pin = NO_PLAN_DEFAULT if no_plan is None else no_plan
    if pin:
        argv.append("--no-plan")
