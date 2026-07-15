# wrapper/scripts/groklib/projectconfig.py
#
# Repo-agnostic project configuration (standalone grok-skills). The wrapper must
# work on ANY target repository, so nothing about the build gate or the rule
# files may be hardcoded to one repo's conventions. This module resolves, per
# target repo root:
#
#   * the package manager for the code-mode build gate, auto-detected from the
#     repo's lockfile (pnpm/yarn/bun/npm), defaulting to npm when only a
#     package.json is present and disabling the JS build gate entirely when no
#     package.json exists anywhere in the target (so `code` still works on a
#     non-JS repo, recording an honest "no build gate ran" warning);
#   * an optional map of workspace names that must NEVER be built (they run a
#     pinned validation command list instead), empty by default;
#   * whether the AGENTS.md/CLAUDE.md rule-file PAIR convention (byte parity +
#     path-header convention) is enforced -- off by default so a plain repo with
#     a single CLAUDE.md (or AGENTS.md, or neither) just works.
#
# Every value can be overridden by an OPTIONAL `.grok-skills.json` at the target
# repo root. A malformed config is a fail-closed validation-failure (it is never
# silently ignored, because a typo that disabled the gate would be a security
# regression). A missing config yields the zero-config defaults.

import dataclasses
import json
import pathlib
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr

CONFIG_FILENAME = ".grok-skills.json"

# Recognized package managers for the build gate, in lockfile detection order.
# Each entry maps a lockfile name to the package-manager token the gate runs.
_LOCKFILE_MANAGERS: Tuple[Tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)

_KNOWN_MANAGERS: Tuple[str, ...] = ("pnpm", "npm", "yarn", "bun")

# When a package.json exists but no lockfile pins a manager, npm is the safe
# universal default (present on every Node install).
_DEFAULT_MANAGER_WITH_MANIFEST = "npm"

_PACKAGE_JSON = "package.json"


def _log(function: str, message: str) -> None:
    log_stderr("projectconfig", function, message)


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    """Resolved per-repo configuration governing the build gate and rule files.

    ``package_manager`` is None when the target repo has no package.json at its
    root, which disables the JS build gate (a non-JS repo still runs `code`, but
    the gate is skipped with an honest warning). ``never_build_workspaces`` maps
    a workspace ``name`` to the exact validation script list it runs instead of
    ``build``. ``require_rule_file_parity`` turns on the strict AGENTS.md/CLAUDE.md
    pair convention (byte parity + path-header validation); off by default.
    """

    package_manager: Optional[str]
    never_build_workspaces: Dict[str, Tuple[str, ...]]
    require_rule_file_parity: bool


def _detect_package_manager(repo_root: pathlib.Path) -> Optional[str]:
    """Detect the package manager from a lockfile at ``repo_root``, else npm when a manifest exists.

    Returns None when the repo root carries neither a recognized lockfile nor a
    package.json, which the caller treats as "no JS build gate for this repo".
    """
    for lockfile, manager in _LOCKFILE_MANAGERS:
        if (repo_root / lockfile).is_file():
            return manager
    if (repo_root / _PACKAGE_JSON).is_file():
        return _DEFAULT_MANAGER_WITH_MANIFEST
    return None


def _read_config_document(repo_root: pathlib.Path) -> Optional[dict]:
    """Read and parse ``.grok-skills.json`` at ``repo_root``, or None when it is absent.

    A present-but-malformed config (unreadable, invalid JSON, or not a JSON
    object) is a fail-closed validation-failure: silently reverting to defaults
    could mask a typo that disabled a safety gate.
    """
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.is_file():
        return None
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log("_read_config_document", "could not read {}: {}".format(config_path, exc))
        raise GrokWrapperError(
            "validation-failure",
            "could not read the project config file: {}".format(CONFIG_FILENAME),
            {"config": str(config_path)},
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        _log("_read_config_document", "invalid JSON in {}: {}".format(config_path, exc))
        raise GrokWrapperError(
            "validation-failure",
            "the project config file is not valid JSON: {}".format(CONFIG_FILENAME),
            {"config": str(config_path)},
        )
    if not isinstance(document, dict):
        raise GrokWrapperError(
            "validation-failure",
            "the project config file must be a JSON object: {}".format(CONFIG_FILENAME),
            {"config": str(config_path)},
        )
    return document


def _resolve_manager_override(document: dict, config_path: pathlib.Path) -> Optional[str]:
    """Return a validated ``packageManager`` override, or None when the key is absent."""
    if "packageManager" not in document:
        return None
    manager = document["packageManager"]
    if manager is None:
        # Explicit null disables the JS build gate for this repo.
        return None
    if not isinstance(manager, str) or manager not in _KNOWN_MANAGERS:
        raise GrokWrapperError(
            "validation-failure",
            "packageManager in {} must be one of {}".format(CONFIG_FILENAME, list(_KNOWN_MANAGERS)),
            {"config": str(config_path), "packageManager": manager if isinstance(manager, str) else None},
        )
    return manager


def _resolve_never_build(document: dict, config_path: pathlib.Path) -> Dict[str, Tuple[str, ...]]:
    """Parse the optional ``neverBuildWorkspaces`` map {name: [script, ...]} with validation."""
    if "neverBuildWorkspaces" not in document:
        return {}
    raw_map = document["neverBuildWorkspaces"]
    if not isinstance(raw_map, dict):
        raise GrokWrapperError(
            "validation-failure",
            "neverBuildWorkspaces in {} must be a JSON object of name -> [scripts]".format(CONFIG_FILENAME),
            {"config": str(config_path)},
        )
    resolved: Dict[str, Tuple[str, ...]] = {}
    for name, scripts in raw_map.items():
        if not isinstance(scripts, list) or not all(isinstance(item, str) and item for item in scripts):
            raise GrokWrapperError(
                "validation-failure",
                "neverBuildWorkspaces[{!r}] in {} must be a list of non-empty script names".format(
                    name, CONFIG_FILENAME
                ),
                {"config": str(config_path), "workspace": name},
            )
        resolved[name] = tuple(scripts)
    return resolved


def _resolve_parity(document: dict, config_path: pathlib.Path) -> bool:
    """Parse the optional boolean ``ruleFileParity`` flag (default False)."""
    if "ruleFileParity" not in document:
        return False
    value = document["ruleFileParity"]
    if not isinstance(value, bool):
        raise GrokWrapperError(
            "validation-failure",
            "ruleFileParity in {} must be a boolean".format(CONFIG_FILENAME),
            {"config": str(config_path)},
        )
    return value


def load_project_config(repo_root: pathlib.Path) -> ProjectConfig:
    """Resolve the effective ProjectConfig for ``repo_root`` (auto-detection + optional overrides)."""
    resolved_root = pathlib.Path(repo_root).resolve()
    document = _read_config_document(resolved_root)
    config_path = resolved_root / CONFIG_FILENAME

    detected_manager = _detect_package_manager(resolved_root)
    never_build: Dict[str, Tuple[str, ...]] = {}
    require_parity = False

    if document is not None:
        if "packageManager" in document:
            detected_manager = _resolve_manager_override(document, config_path)
        never_build = _resolve_never_build(document, config_path)
        require_parity = _resolve_parity(document, config_path)

    return ProjectConfig(
        package_manager=detected_manager,
        never_build_workspaces=never_build,
        require_rule_file_parity=require_parity,
    )


def install_command(package_manager: str) -> List[str]:
    """Return the offline, lockfile-frozen dependency-install argv for ``package_manager``.

    Best-effort convenience so the build gate has node_modules present; every
    variant is forced OFFLINE and lockfile-frozen so the install never reaches the
    network and never mutates the lockfile.
    """
    if package_manager == "npm":
        return ["npm", "install", "--offline", "--no-audit", "--no-fund"]
    if package_manager == "yarn":
        return ["yarn", "install", "--offline", "--frozen-lockfile"]
    if package_manager == "bun":
        return ["bun", "install", "--frozen-lockfile"]
    return ["pnpm", "install", "--offline", "--frozen-lockfile"]


def build_gate_command(package_manager: str, script: str) -> List[str]:
    """Return the location-pinned argv that runs ``script`` via ``package_manager``.

    The command is run with cwd set to the target workspace directory (the caller
    supplies the cwd), which pins the gate to the workspace by LOCATION -- a Grok
    rename of the package.json ``name`` can never redirect it onto a different
    package, exactly like a path filter, but in a package-manager-agnostic way.
    yarn runs a script as ``yarn <script>``; npm/pnpm/bun use ``<pm> run <script>``.
    """
    if package_manager == "yarn":
        return ["yarn", script]
    return [package_manager, "run", script]
