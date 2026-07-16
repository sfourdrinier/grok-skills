# wrapper/scripts/groklib/platformsupport.py
#
# D-PORT (2026-07-14): the single home for every OS-specific primitive the
# skill uses. Every other module (runstate, authhome, sandbox, and Task 7's
# grokcli) routes its POSIX-only calls (chmod-based permission hardening,
# os.getuid / st_uid ownership checks, os.killpg / start_new_session process
# groups, O_EXCL private-file creation) through this abstraction so the whole
# skill runs on Linux and Windows with a real, non-crashing branch, never a
# stub and never a raise NotImplementedError.
#
# This is the ONLY module in the package permitted to read sys.platform /
# os.name; everywhere else calls current_platform() / is_posix(). Every
# public function delegates to a platform-parameterized internal helper
# (_<name>_for(platform, ...)) so the non-host branch is unit-tested by
# injecting the platform directly, without a real Linux/Windows box.
#
# Windows filesystem hardening: Python's stdlib has no native ACL API and
# hand-written ctypes SetEntriesInAcl code cannot be validated on this macOS
# host, so restrict_*_permissions attempt the documented Windows built-in
# `icacls` (break inheritance, grant only the current user) and, if that is
# unavailable or fails, fall back to the documented reliance on the per-user
# profile temp directory -- which Windows already ACL-protects to the owning
# user -- logging the residual rather than shipping unverifiable ACL code.
# The exclusive-creation security property (O_EXCL, 0600 birth mode on POSIX;
# O_EXCL create inside the per-user temp dir on Windows) is airtight on every
# platform regardless of whether the best-effort ACL tighten succeeds.
#
# Import isolation (C5): stdlib plus groklib.__init__ only. runstate is
# allowed to import this module precisely because it pulls in nothing from
# argparse / modes / grokcli / sandbox / rules / envelope.

import os
import pathlib
import signal
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from groklib import GrokWrapperError, log_stderr

_DIR_MODE = 0o700
_FILE_MODE = 0o600

# CREATE_NEW_PROCESS_GROUP is a Windows-only subprocess constant absent from
# the subprocess module on POSIX, so the numeric value is inlined (with a
# getattr fallback) to build the Windows spawn kwargs on any host without an
# AttributeError.
_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

# Only macOS has a captured Grok sandbox probe report in v1; live modes and
# sandbox.verify_enforcement fail closed on every other platform until its own
# probe suite runs and captures a sandbox report (the version-revalidation
# pattern, applied per platform).
PROBED_PLATFORMS: Tuple[str, ...] = ("macos",)

# The ProfileApplied.platform label a captured probe report pins for each
# probed platform. verify_enforcement compares the telemetry's platform field
# against this instead of hardcoding "macos/seatbelt". Only probed platforms
# have an entry; expected_sandbox_platform is reached only after
# require_probed_platform_for_live has already gated the platform.
_EXPECTED_SANDBOX_PLATFORM_BY_PLATFORM: Dict[str, str] = {
    "macos": "macos/seatbelt",
}

# Mandatory per-run session-temp roots the sandbox is always allowed to write
# under (the private Grok home and OS scratch live here). Compared with
# realpath normalization so the macOS /var -> /private/var symlink and the
# /tmp -> /private/tmp symlink are handled. Dynamic per-user roots
# (XDG_RUNTIME_DIR, %TEMP%) are appended at call time.
_SESSION_TEMP_ROOTS_BY_PLATFORM: Dict[str, Tuple[str, ...]] = {
    "macos": ("/private/var/folders", "/tmp", "/var/tmp", "/private/tmp"),
    "linux": ("/tmp", "/var/tmp"),
    "windows": (),
}

# Per-platform real-home credential directories rendered under
# deny_read_globs as a best-effort, defense-in-depth artifact (D-SECRETREAD:
# grok 0.2.101 does not enforce read denial; write confinement is the only
# enforced boundary). Home-relative names are joined to the operator's real
# home; platform-absolute credential stores (macOS system keychain, Windows
# credential dirs) are added per platform.
_CREDENTIAL_HOME_RELATIVE_DIRS_BY_PLATFORM: Dict[str, Tuple[str, ...]] = {
    "macos": (".ssh", ".aws", ".grok", ".config"),
    "linux": (".ssh", ".aws", ".grok", ".config", ".gnupg"),
    "windows": (".ssh", ".aws", ".grok"),
}
_MACOS_USER_KEYCHAIN_RELATIVE_DIR = "Library/Keychains"
_MACOS_SYSTEM_KEYCHAIN_DIR = "/Library/Keychains"


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "platformsupport" component prefix."""
    log_stderr("platformsupport", function, message)


def _platform_from(os_name: str, sys_platform: str) -> str:
    """Pure mapping from (os.name, sys.platform) to one of macos|linux|windows.

    Windows is detected first (os.name "nt"); macOS is "darwin"; everything
    else on a POSIX system is treated as linux. Kept pure so both non-host
    branches are unit-testable by injecting the pair directly.
    """
    if os_name == "nt" or sys_platform.startswith("win"):
        return "windows"
    if sys_platform == "darwin":
        return "macos"
    return "linux"


def current_platform() -> str:
    """Return this host's platform: "macos" | "linux" | "windows"."""
    return _platform_from(os.name, sys.platform)


def _is_posix_platform(platform: str) -> bool:
    """True for the POSIX platforms (macos, linux); False for windows."""
    return platform in ("macos", "linux")


def is_posix() -> bool:
    """True when this host is a POSIX platform (macOS or Linux)."""
    return _is_posix_platform(current_platform())


def _current_windows_principal() -> Optional[str]:
    """Resolve the current Windows principal from the PROCESS TOKEN via ``whoami``.

    Grok r3 #10 windows-acl-spoofable-env: the previous principal was built from
    the ``USERNAME``/``USERDOMAIN`` environment variables, which a caller can
    SPOOF before launch to make ``icacls`` grant an UNINTENDED principal. ``whoami``
    reads the actual process access token (``DOMAIN\\user``), NOT the environment,
    so it cannot be redirected by a spoofed env var. Returns the ``DOMAIN\\user``
    string, or None on any failure (whoami unavailable / non-zero / empty) -- in
    which case the caller GRANTS NOTHING and relies on the per-user profile temp
    ACL (fail closed: never grant to a spoofable principal). Never raises.
    """
    try:
        completed = subprocess.run(
            ["whoami"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError as exc:
        _log("_current_windows_principal", "whoami unavailable: {}".format(exc))
        return None
    if completed.returncode != 0:
        _log("_current_windows_principal", "whoami exited {}".format(completed.returncode))
        return None
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    return lines[0] if lines else None


def _restrict_via_windows_acl(path: pathlib.Path, is_dir: bool) -> bool:
    """Restrict ``path`` to the current user via the Windows built-in ``icacls``.

    Breaks ACL inheritance and grants full control to only the current user
    (adding container/object inheritance for directories). The principal is
    resolved from the PROCESS TOKEN via ``whoami`` (never the spoofable
    USERNAME/USERDOMAIN env, Grok r3 #10). Returns True on success, False (logged)
    when the principal cannot be resolved from the token, icacls is unavailable, or
    icacls reports a non-zero status -- in which case the caller relies on the
    documented per-user profile temp dir protection. Never raises.
    """
    principal = _current_windows_principal()
    if not principal:
        _log(
            "_restrict_via_windows_acl",
            "cannot resolve current Windows principal from the process token for {}".format(path),
        )
        return False

    grant = "{}:(OI)(CI)F".format(principal) if is_dir else "{}:F".format(principal)
    try:
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        _log("_restrict_via_windows_acl", "icacls unavailable for {}: {}".format(path, exc))
        return False

    if completed.returncode != 0:
        _log(
            "_restrict_via_windows_acl",
            "icacls returned {} for {}".format(completed.returncode, path),
        )
        return False
    return True


def _restrict_permissions_for(platform: str, path: pathlib.Path, mode: int, is_dir: bool) -> None:
    """Harden ``path`` to owner-only per platform: POSIX chmod, Windows ACL/fallback."""
    if _is_posix_platform(platform):
        try:
            os.chmod(str(path), mode)
        except OSError as exc:
            _log("_restrict_permissions_for", "chmod {:o} failed for {}: {}".format(mode, path, exc))
            raise
        return

    if not _restrict_via_windows_acl(path, is_dir):
        # Documented residual: the per-user profile temp directory is already
        # ACL-restricted by Windows to the owning user, so the private home
        # created inside it stays owner-confined even when the belt-and-braces
        # icacls tighten could not run.
        _log(
            "_restrict_permissions_for",
            "relying on per-user profile temp ACL for {} (icacls unavailable)".format(path),
        )


def restrict_dir_permissions(path: pathlib.Path) -> None:
    """Restrict ``path`` (a directory) to the current user only (POSIX 0700 / Windows ACL)."""
    _restrict_permissions_for(current_platform(), path, _DIR_MODE, is_dir=True)


def restrict_file_permissions(path: pathlib.Path) -> None:
    """Restrict ``path`` (a file) to the current user only (POSIX 0600 / Windows ACL)."""
    _restrict_permissions_for(current_platform(), path, _FILE_MODE, is_dir=False)


def _open_private_file_for(platform: str, path: pathlib.Path) -> int:
    """Exclusively create ``path`` owner-private and return its write file descriptor.

    O_WRONLY|O_CREAT|O_EXCL guarantees the file is created fresh by us on
    every platform (a pre-existing path is refused with FileExistsError, so an
    attacker-planted file is never adopted). POSIX births the file at 0600.
    Windows opens in binary mode (Python's io layer owns newline translation)
    and then best-effort tightens the ACL; the per-user temp dir already
    confines it regardless.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if not _is_posix_platform(platform):
        flags = flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)

    file_descriptor = os.open(str(path), flags, _FILE_MODE)

    if not _is_posix_platform(platform):
        if not _restrict_via_windows_acl(path, is_dir=False):
            _log(
                "_open_private_file_for",
                "relying on per-user profile temp ACL for {} (icacls unavailable)".format(path),
            )
    return file_descriptor


def open_private_file(path: pathlib.Path) -> int:
    """Exclusively create ``path`` as an owner-private file, returning its write fd (O_EXCL)."""
    return _open_private_file_for(current_platform(), path)


def _owning_uid_for(platform: str) -> Optional[int]:
    """Current-process uid on POSIX; None on Windows (no uid concept, os.getuid absent)."""
    if _is_posix_platform(platform):
        return os.getuid()
    return None


def owning_uid_or_none() -> Optional[int]:
    """Return the current process uid on POSIX, or None on Windows."""
    return _owning_uid_for(current_platform())


def _path_owned_for(platform: str, st: os.stat_result) -> bool:
    """POSIX: st_uid == current uid. Windows: best-effort True (per-user temp reliance).

    The current uid comes from ``_owning_uid_for`` (the same platform-parameterized
    helper ``owning_uid_or_none`` delegates to), so the ownership abstraction is
    single-sourced instead of calling ``os.getuid`` directly (T2).
    """
    current_uid = _owning_uid_for(platform)
    if current_uid is not None:
        return st.st_uid == current_uid
    # Windows stat_result carries no meaningful st_uid (owning_uid returns None);
    # the only caller scans the per-user profile temp dir, which Windows
    # ACL-restricts to the current user, so anything found there is treated as owned.
    return True


def path_is_owned_by_current_user(st: os.stat_result) -> bool:
    """True when ``st`` is owned by the current user (POSIX uid check; Windows best-effort)."""
    return _path_owned_for(current_platform(), st)


def _process_is_alive_for(platform: str, pid: int) -> bool:
    """True when a process with ``pid`` is currently alive on ``platform``.

    POSIX: ``os.kill(pid, 0)`` sends no signal but performs the existence +
    permission check -- alive when it returns or raises PermissionError (the
    process exists but is owned by another user), dead on ProcessLookupError.
    Windows: ``os.kill`` does NOT support signal 0 (it would terminate the
    process), so it is never called there; liveness is reported False so
    age-based stale-home reaping still runs. Windows live modes are gated off
    (require_probed_platform_for_live), so no ACTIVE home exists to protect and
    this best-effort branch cannot delete a live run's home.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if _is_posix_platform(platform):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            # Unexpected probe error: fail closed as alive so reapers and
            # finalize-parent recovery never treat an inconclusive probe as dead.
            _log(
                "_process_is_alive_for",
                "os.kill(0) inconclusive for pid {} (treating as alive): {}".format(pid, exc),
            )
            return True
        return True
    return False


def process_is_alive(pid: int) -> bool:
    """True when a process with ``pid`` is currently alive on this host (POSIX kill(0) probe)."""
    return _process_is_alive_for(current_platform(), pid)


def _process_start_token_for(platform: str, pid: int) -> Optional[str]:
    """Best-effort STABLE per-process identity token (its start time) for ``pid``.

    Binds a liveness lease to the SPECIFIC process that wrote it: two processes
    that share a pid across a recycle have different start times, so comparing the
    stored token against the live pid's current token detects pid reuse (a dead
    run whose pid was recycled must not look alive). Returns None when the token
    cannot be obtained (an unprobed reader, a permission edge, ps unavailable);
    the caller then falls back to bare pid existence, which stays CONSERVATIVE:
    an unverifiable but live pid is treated as still-active, never reaped.

      Linux: field 22 (starttime) of /proc/<pid>/stat -- the comm field (2) is
        parenthesized and may itself contain spaces/parens, so parse after the
        LAST ')'; starttime is the 20th whitespace field of that tail.
      macOS: `ps -o lstart= -p <pid>` (no /proc) under a PINNED TZ=UTC / LC_ALL=C
        environment so the human start timestamp renders identically for a given
        process regardless of the reader's timezone/locale, with internal
        whitespace collapsed -- a stable, parse-safe identity discriminator for the
        lifetime of the process (Round7 F1: the un-pinned rendering varied by TZ).
      Windows: None -- live modes are gated off, so no active home needs binding.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if platform == "linux":
        try:
            with open("/proc/{}/stat".format(pid), "r", encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            _log("_process_start_token_for", "cannot read /proc/{}/stat: {}".format(pid, exc))
            return None
        rparen = content.rfind(")")
        if rparen == -1:
            return None
        tail_fields = content[rparen + 1:].split()
        # tail_fields[0] is state (field 3); starttime (field 22) is index 19.
        if len(tail_fields) <= 19:
            return None
        return tail_fields[19]
    if platform == "macos":
        # `ps -o lstart=` renders the process start time as a human wall-clock
        # string in the CALLER's timezone and locale, so the SAME live process
        # yields DIFFERENT text under a different TZ / LC_* (Round7 F1). The
        # liveness lease's identity comparison would then wrongly classify a live,
        # non-recycled owner as recycled (dead). Pin TZ=UTC and LC_ALL=C so the
        # rendering is deterministic for a given process regardless of the reader's
        # environment, and collapse whitespace runs so field padding cannot vary
        # the token either -- a stable, parse-safe per-process identity.
        normalized_env = dict(os.environ)
        normalized_env["TZ"] = "UTC"
        normalized_env["LC_ALL"] = "C"
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                env=normalized_env,
            )
        except OSError as exc:
            _log("_process_start_token_for", "ps unavailable for pid {}: {}".format(pid, exc))
            return None
        if completed.returncode != 0:
            return None
        raw = (completed.stdout or "").strip()
        if not raw:
            return None
        return " ".join(raw.split())
    return None


def process_start_token(pid: int) -> Optional[str]:
    """Return a stable per-process identity token (start time) for ``pid``, or None."""
    return _process_start_token_for(current_platform(), pid)


def _spawn_kwargs_for(platform: str) -> Dict[str, object]:
    """subprocess.Popen kwargs that put the child in its own process group per platform."""
    if _is_posix_platform(platform):
        return {"start_new_session": True}
    return {"creationflags": _WINDOWS_CREATE_NEW_PROCESS_GROUP}


def spawn_kwargs_new_group() -> Dict[str, object]:
    """Return Popen kwargs starting the child in a new process group (POSIX session / Windows group)."""
    return _spawn_kwargs_for(current_platform())


def _kill_process_tree_for(platform: str, proc: "subprocess.Popen") -> None:
    """Kill ``proc`` and its whole group: POSIX killpg(SIGKILL); Windows taskkill /T /F then terminate.

    Every failure is logged and swallowed with an explicit decision: a
    process that has already exited is not an error, and the caller reaps the
    child afterwards, so a best-effort kill must never itself raise.
    """
    pid = proc.pid
    if _is_posix_platform(platform):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError as exc:
            # Already-exited group (ProcessLookupError) or a permissions/EPERM
            # edge: nothing left to kill, caller still reaps. Log and proceed.
            _log("_kill_process_tree_for", "killpg failed for pid {}: {}".format(pid, exc))
        return

    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        _log("_kill_process_tree_for", "taskkill unavailable for pid {}: {}".format(pid, exc))

    try:
        proc.terminate()
    except OSError as exc:
        # Belt-and-braces after taskkill; a missing process here is benign.
        _log("_kill_process_tree_for", "terminate fallback failed for pid {}: {}".format(pid, exc))


def kill_process_tree(proc: "subprocess.Popen") -> None:
    """Force-kill ``proc`` and every child in its process group (POSIX killpg / Windows taskkill)."""
    _kill_process_tree_for(current_platform(), proc)


def require_probed_platform_for_live() -> None:
    """Fail closed (probe-required) when this host has no captured Grok sandbox probe report.

    macOS is the only probed platform in v1; live modes and
    sandbox.verify_enforcement call this first so Linux/Windows stay blocked
    until their own probe suite runs. The raised message names the platform.
    """
    platform = current_platform()
    if platform not in PROBED_PLATFORMS:
        _log(
            "require_probed_platform_for_live",
            "platform {} has no captured Grok sandbox probe report".format(platform),
        )
        raise GrokWrapperError(
            "probe-required",
            "live sandbox mode is blocked on platform {}: no captured Grok sandbox probe report".format(
                platform
            ),
            {"platform": platform, "probedPlatforms": list(PROBED_PLATFORMS)},
        )


def _expected_sandbox_platform_for(platform: str) -> str:
    """Return the ProfileApplied.platform label the probe report pins for ``platform``."""
    label = _EXPECTED_SANDBOX_PLATFORM_BY_PLATFORM.get(platform)
    if label is None:
        # Only reachable if called for an unprobed platform without the
        # require_probed_platform_for_live gate; fail closed rather than guess.
        _log("_expected_sandbox_platform_for", "no probed sandbox platform label for {}".format(platform))
        raise GrokWrapperError(
            "probe-required",
            "no captured sandbox platform label for platform {}".format(platform),
            {"platform": platform},
        )
    return label


def expected_sandbox_platform() -> str:
    """Return the expected sandbox ProfileApplied.platform label for this probed host."""
    return _expected_sandbox_platform_for(current_platform())


def _mandatory_session_temp_roots_for(platform: str) -> Tuple[str, ...]:
    """Per-platform session-temp roots the sandbox may always write under."""
    roots: List[str] = list(_SESSION_TEMP_ROOTS_BY_PLATFORM.get(platform, ()))
    if platform == "linux":
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
        if xdg_runtime_dir:
            roots.append(xdg_runtime_dir)
    elif platform == "windows":
        windows_temp = os.environ.get("TEMP", "") or os.environ.get("TMP", "")
        if windows_temp:
            roots.append(windows_temp)
    return tuple(roots)


def mandatory_session_temp_roots() -> Tuple[str, ...]:
    """Return the session-temp roots the sandbox is always allowed to write under, this platform."""
    return _mandatory_session_temp_roots_for(current_platform())


def _credential_deny_dirs_for(platform: str, real_home: pathlib.Path) -> List[str]:
    """Per-platform absolute real-home credential directories to list under deny_read_globs."""
    relative_dirs = _CREDENTIAL_HOME_RELATIVE_DIRS_BY_PLATFORM.get(
        platform, (".ssh", ".aws", ".grok", ".config")
    )
    dirs: List[str] = [str(real_home / relative_dir) for relative_dir in relative_dirs]

    if platform == "macos":
        dirs.append(str(real_home / _MACOS_USER_KEYCHAIN_RELATIVE_DIR))
        dirs.append(_MACOS_SYSTEM_KEYCHAIN_DIR)
    elif platform == "windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            dirs.append(str(pathlib.Path(appdata) / "Microsoft" / "Credentials"))
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            dirs.append(str(pathlib.Path(local_appdata) / "Microsoft" / "Credentials"))
    return dirs


def credential_deny_dirs(real_home: pathlib.Path) -> List[str]:
    """Return the operator's absolute real-home credential directories for this platform."""
    return _credential_deny_dirs_for(current_platform(), real_home)
