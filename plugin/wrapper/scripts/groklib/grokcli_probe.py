# wrapper/scripts/groklib/grokcli_probe.py
#
# Read-only Grok probes (`grok inspect --json`, `grok models`) used by preflight
# to prove the private home's config surface and login/model state. Extracted
# from grokcli.py (900-line cap) as a cohesive unit: the two probes plus the
# shared read-only runner that spawns them in their own process group and force-
# kills a hung probe as a whole tree on timeout.
#
# The runner reuses grokcli's minimal-child-env construction and its
# SIGTERM-safe active-process registry through the ``grokcli`` module (module-
# attribute access, so the SAME _ACTIVE_PROCS set is shared and test patches of
# those seams still apply). grokcli does NOT import this module, so the
# dependency is strictly one-directional (grokcli_probe -> grokcli).

import pathlib
import subprocess
from typing import Dict, List, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import grokcli
from groklib import grokcli_output
from groklib import platformsupport
from groklib.authhome import PrivateHome
from groklib.envelope import redact_secret_value_text


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "grokcli_probe" component prefix."""
    log_stderr("grokcli_probe", function, message)


def _run_read_only_probe(
    binary: pathlib.Path, home: PrivateHome, subcommand_argv: List[str], timeout_seconds: int, probe_name: str
) -> Tuple[int, str, str]:
    """Run a read-only grok probe (inspect/models) in the C6 minimal child env.

    Shared by inspect_home and probe_login: constructs the same minimal env
    and runs from the private home in its OWN process group
    (platformsupport.spawn_kwargs_new_group), so a hung probe is force-killed as
    a WHOLE tree via platformsupport.kill_process_tree on timeout -- exactly like
    execute (S3, D-PORT: no raw os.killpg here). A spawn failure is a fail-closed
    ``output-malformed`` (unparseable because it never ran); a timeout is a
    fail-closed ``timeout``. Returns (returncode, stdout, stderr); the caller
    checks the returncode.
    """
    env = grokcli._minimal_env(home, binary)
    grokcli._ensure_private_tmp(pathlib.Path(env["TMPDIR"]))
    argv = [str(binary)] + subcommand_argv
    # Spawn and register ATOMICALLY under a SIGTERM block so a SIGTERM can never
    # orphan a live probe child in the window between Popen and registration
    # (F1-signal-race-orphan-grok-child).
    with grokcli._sigterm_blocked():
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=str(home.home_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                **platformsupport.spawn_kwargs_new_group(),
            )
        except OSError as exc:
            _log("_run_read_only_probe", "could not spawn grok {}: {}".format(probe_name, exc))
            raise GrokWrapperError(
                "output-malformed",
                "could not run grok {}".format(probe_name),
                {"reason": "{}-command-failed".format(probe_name)},
            )
        grokcli._register_active_proc(proc)

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _log(
                "_run_read_only_probe",
                "grok {} exceeded {}s timeout; killing process tree".format(probe_name, timeout_seconds),
            )
            platformsupport.kill_process_tree(proc)
            grokcli._reap_after_kill(proc)
            raise GrokWrapperError(
                "timeout",
                "grok {} probe exceeded its {} second timeout".format(probe_name, timeout_seconds),
                {"timeoutSeconds": timeout_seconds, "probe": probe_name},
            )
    finally:
        grokcli._unregister_active_proc(proc)

    return proc.returncode, stdout or "", stderr or ""


def inspect_home(binary: pathlib.Path, home: PrivateHome, leader_socket: pathlib.Path) -> Dict[str, object]:
    """Run `grok inspect --json` in the private home and return its config surface.

    Task 0 (probe-report.md Step 2, deviation 3) proved `grok inspect --json`
    carries the config surface (permissions, hooks, plugins, mcpServers,
    configSources) but NO login/model/tool data, so this returns exactly that
    surface. A nonzero exit is a fail-closed ``cli-failure`` (even if the output
    happens to parse); unparseable output is ``output-malformed``.
    """
    returncode, stdout, stderr = _run_read_only_probe(
        binary,
        home,
        ["inspect", "--json", "--leader-socket", str(leader_socket)],
        grokcli._INSPECT_TIMEOUT_SECONDS,
        "inspect",
    )
    if returncode != 0:
        _log("inspect_home", "grok inspect exited {}".format(returncode))
        raise GrokWrapperError(
            "cli-failure",
            "grok inspect exited with status {}".format(returncode),
            {"exitStatus": returncode, "probe": "inspect", "stderr": redact_secret_value_text(stderr)},
        )
    parsed = grokcli_output.parse_grok_json(stdout)
    return {
        "grokVersion": parsed.get("grokVersion"),
        "permissions": parsed.get("permissions"),
        "hooks": parsed.get("hooks"),
        "plugins": parsed.get("plugins"),
        "mcpServers": parsed.get("mcpServers"),
        "configSources": parsed.get("configSources"),
    }


def probe_login(binary: pathlib.Path, home: PrivateHome, leader_socket: pathlib.Path) -> Dict[str, object]:
    """Run `grok models` in the private home and parse login state plus the selectable models.

    Task 0 (probe-report.md Step 2) proved login state and the model list come
    from `grok models`, not `grok inspect`. A nonzero exit is a fail-closed
    ``cli-failure`` (even if the output happens to parse); not-logged-in is a
    fail-closed ``auth-missing``; unparseable-but-logged-in output is
    ``output-malformed``.
    """
    returncode, stdout, stderr = _run_read_only_probe(
        binary,
        home,
        ["models", "--leader-socket", str(leader_socket)],
        grokcli._MODELS_TIMEOUT_SECONDS,
        "models",
    )
    if returncode != 0:
        _log("probe_login", "grok models exited {}".format(returncode))
        raise GrokWrapperError(
            "cli-failure",
            "grok models exited with status {}".format(returncode),
            {"exitStatus": returncode, "probe": "models", "stderr": redact_secret_value_text(stderr)},
        )
    return grokcli_output.parse_models_output(stdout)
