# wrapper/scripts/groklib/web_defaults.py
#
# Single source of truth for per-mode web-search defaults (Wave 1 C-A3 /
# D-W1-GROUND). Callers pass the explicit --web / --no-web resolution result
# (None when neither flag was set) and get a bool.

from typing import Optional

# Default-on: reason (cold opinion benefits from live docs). Review/code stay
# opt-in. Verify is always hermetic (never web). adversarial-review is a
# companion framing of review that forces --web before the wrapper runs.
DEFAULT_WEB_BY_MODE = {
    "preflight": False,
    "review": False,
    # Default off: reason often carries --input secrets; web is opt-in via --web.
    "reason": False,
    "code": False,
    "verify": False,
    "status": False,
    "cleanup": False,
}


def resolve_web_access(mode: str, web_flag: Optional[bool]) -> bool:
    """Resolve effective web access for ``mode``.

    ``web_flag`` is True when --web was passed, False when --no-web was passed,
    and None when neither flag was set (use the mode default table).
    """
    if mode == "verify":
        return False
    if web_flag is True:
        return True
    if web_flag is False:
        return False
    return bool(DEFAULT_WEB_BY_MODE.get(mode, False))
