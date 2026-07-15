# wrapper/scripts/groklib/authhome.py
#
# C2 private-home isolation: builds and destroys the per-run private Grok
# HOME directory that isolates real authentication material away from
# whatever working directory a Grok child process is invoked against.
# create_private_home copies the operator's real ~/.grok auth material into
# a fresh, exclusively-owned temp directory (0700 home, 0600 file copies)
# and writes the wrapper-generated config.toml (and optionally Task 6's
# sandbox.toml) alongside it. destroy_private_home tears the private home
# back down with the authentication material copies removed FIRST, before
# any other cleanup step, and never raises for residue that is not
# authentication material.
#
# Secrets discipline (absolute, per the Global Constraints): no function in
# this module ever reads authentication file bytes into a variable that is
# logged, returned, or included in any exception message. Auth file copies
# stream bytes directly from an opened source file descriptor to a
# destination descriptor (shutil.copyfileobj over the two open file
# objects) without materializing them as a Python value this module could
# accidentally leak; the destination descriptor is opened O_EXCL at mode
# 0600 from the very first instant it exists, so it is never observable at
# a wider mode during the copy.
#
# D4(a) deliberate, bounded exception: after the auth material is copied in,
# create_private_home delegates to groklib.injectedsecrets to capture the exact
# injected credential value(s) into a per-run redaction denylist, so the
# envelope builder can mask any exact echo of them from stdout. Those captured
# values live ONLY in injectedsecrets' private per-run global; this module never
# logs them, never returns them (PrivateHome carries no auth-derived value), and
# never embeds them in an exception message. Extraction is fail-safe: it never
# raises, so it can never leave a private home half-built.

import dataclasses
import os
import pathlib
import re
import shutil
import stat
import tempfile
from typing import List

from groklib import GrokWrapperError, log_stderr
from groklib import injectedsecrets
from groklib import platformsupport
from groklib.runstate import (
    TEMP_HOME_PREFIX,
    new_run_id,
    write_home_liveness_marker,
    write_owner_marker,
)

# C2: "tempfile.mkdtemp(prefix=TEMP_HOME_PREFIX)". The private-home name prefix
# is defined ONCE in groklib.runstate (which also filters its stale-home audit
# by it) and imported here (DRY), so the mkdtemp prefix and the audit scan can
# never drift apart. Importing runstate here does not violate runstate's C5
# import-isolation: authhome depends on runstate, never the reverse.
# The 0600 file birth-mode lives inside platformsupport.open_private_file, so
# this module keeps only the directory birth-mode constant.
_DIR_MODE = 0o700

_GROK_DIR_NAME = ".grok"
_CONFIG_TOML_FILENAME = "config.toml"
_SANDBOX_TOML_FILENAME = "sandbox.toml"

# Filenames create_private_home writes itself DIRECTLY under .grok/; these are
# never copied authentication material. destroy_private_home treats every OTHER
# regular file found ANYWHERE under .grok/ (including nested subdirectories) as an
# authentication material copy that must be removed FIRST, before these top-level
# wrapper-generated files or the general directory teardown (Grok r3 #13
# nested-grok-auth-classification: a credential file Grok wrote under .grok/<sub>/
# is auth material too, not merely "residual non-auth").
_WRAPPER_GENERATED_GROK_FILENAMES = frozenset({_CONFIG_TOML_FILENAME, _SANDBOX_TOML_FILENAME})


def _enumerate_grok_auth_files(grok_dir: pathlib.Path) -> "tuple[List[pathlib.Path], bool]":
    """Return (auth-material file paths anywhere under ``grok_dir``, enumeration_failed).

    Auth material = every REGULAR file under ``grok_dir`` at ANY depth EXCEPT the
    two top-level wrapper-generated files (config.toml / sandbox.toml). A nested
    credential file (``.grok/<sub>/token.json``) is therefore removed FIRST and
    classified auth-specifically, not left for the final rmtree and mis-reported as
    "residual non-auth" (Grok r3 #13). Non-regular entries (the leader socket, a
    symlink) are left for the final rmtree exactly as before. A walk error fails
    closed: it cannot verify whether auth material remains, so enumeration_failed
    is True and the caller reports "failed" rather than a false "clean". A file
    that cannot be lstat'd is treated as auth-shaped (fail closed).
    """
    auth_paths: List[pathlib.Path] = []
    enumeration_failed = False

    def _on_walk_error(exc: OSError) -> None:
        nonlocal enumeration_failed
        _log_stderr("_enumerate_grok_auth_files", "failed to enumerate under {}: {}".format(grok_dir, exc))
        enumeration_failed = True

    for root, _dirs, files in os.walk(str(grok_dir), onerror=_on_walk_error):
        root_path = pathlib.Path(root)
        is_top_level = root_path == grok_dir
        for filename in sorted(files):
            if is_top_level and filename in _WRAPPER_GENERATED_GROK_FILENAMES:
                continue
            file_path = root_path / filename
            try:
                is_regular = stat.S_ISREG(os.lstat(str(file_path)).st_mode)
            except OSError:
                is_regular = True  # cannot classify -> treat as auth (fail closed)
            if is_regular:
                auth_paths.append(file_path)

    return auth_paths, enumeration_failed


def _log_stderr(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "authhome" component prefix."""
    log_stderr("authhome", function, message)


@dataclasses.dataclass(frozen=True)
class PrivateHome:
    home_dir: pathlib.Path
    grok_dir: pathlib.Path
    config_path: pathlib.Path


def _mkdir_new_0700(path: pathlib.Path) -> None:
    """Create a brand-new directory at ``path`` (must not already exist), owner-private.

    On POSIX the directory is forced to exactly 0700; on Windows the
    platformsupport abstraction applies the ACL equivalent (or the documented
    per-user profile temp reliance).
    """
    os.mkdir(str(path), _DIR_MODE)
    platformsupport.restrict_dir_permissions(path)


def _write_text_0600(path: pathlib.Path, content: str) -> None:
    """Write ``content`` to ``path`` as UTF-8, owner-private and created exclusively.

    Routes through ``platformsupport.open_private_file`` (O_WRONLY|O_CREAT|
    O_EXCL, 0600 birth mode on POSIX), so the wrapper-generated config.toml /
    sandbox.toml are created fresh in the private home and never adopt a
    pre-existing file. Both are written exactly once into a freshly-minted
    home, so exclusive creation never conflicts. A trailing
    ``restrict_file_permissions`` is umask-independent belt-and-braces on top
    of the already-0600 descriptor mode.
    """
    file_descriptor = platformsupport.open_private_file(path)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    platformsupport.restrict_file_permissions(path)


def _copy_auth_file_0600(source_path: pathlib.Path, dest_path: pathlib.Path) -> None:
    """Stream-copy one authentication file into ``dest_path``, 0600 from the first instant it exists.

    Unlike ``shutil.copyfile`` followed by a separate ``os.chmod`` (which
    leaves a window where the freshly-created destination exists at the
    umask-derived mode before the chmod call runs), this opens the
    destination descriptor through ``platformsupport.open_private_file``
    (``O_WRONLY | O_CREAT | O_EXCL`` and mode 0600 on POSIX; O_EXCL exclusive
    creation inside the per-user temp dir on Windows) before a single byte is
    written, so the destination can never be observed at a mode wider than
    0600 at any instant and a pre-existing destination is refused rather than
    adopted. The source is opened first so a missing/unreadable source fails
    before the destination file is even created. ``shutil.copyfileobj``
    streams the authentication bytes through the copy buffer only; they are
    never materialized as a Python value this module could log, return, or
    store. The trailing ``restrict_file_permissions`` is umask-independent
    belt-and-braces on top of the already-0600 descriptor mode.
    """
    with open(str(source_path), "rb") as source_handle:
        destination_descriptor = platformsupport.open_private_file(dest_path)
        with os.fdopen(destination_descriptor, "wb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle)
    platformsupport.restrict_file_permissions(dest_path)


def _best_effort_discard_partial_home(home_dir: pathlib.Path) -> None:
    """Best-effort removal of a partially-built private home after a construction failure.

    Never raises: a failure here must not mask the original construction
    error that triggered it. Still logged, so operators know a temp home
    may be left for runstate.audit_stale_temp_homes to reap later.
    """

    def _on_error(_func: object, failed_path: str, _exc_info: object) -> None:
        _log_stderr(
            "_best_effort_discard_partial_home",
            "failed to remove residual path {} while discarding partial home".format(failed_path),
        )

    try:
        shutil.rmtree(str(home_dir), onerror=_on_error)
    except OSError as exc:
        _log_stderr(
            "_best_effort_discard_partial_home",
            "rmtree raised while discarding partial home {}: {}".format(home_dir, exc),
        )


def create_private_home(
    *,
    source_grok_dir: pathlib.Path,
    auth_file_names: "tuple[str, ...]",
    config_toml: str,
    sandbox_toml: "str|None" = None,
) -> PrivateHome:
    """Build a fresh, exclusively-owned private Grok HOME per C2.

    Every name in ``auth_file_names`` must exist as a regular file directly
    under ``source_grok_dir``; this is verified BEFORE any temp directory is
    created, so a missing auth file fails closed with zero filesystem side
    effects (``GrokWrapperError("auth-missing")``, never a partially-built
    temp home left behind).

    Each present auth file is streamed byte-for-byte into
    ``<home>/.grok/<name>`` via ``_copy_auth_file_0600`` (never
    ``shutil.copy``/``copy2``, which would also propagate the source's mode
    bits, and never a copy-then-chmod sequence, which would leave a window
    where the destination exists at the umask-derived mode): the
    destination descriptor is created 0600 from the instant it exists, then
    explicitly ``os.chmod``'d to 0600 again as umask-independent
    belt-and-braces. This module never trusts ``copystat`` to produce the
    correct mode from a source file whose permissions it does not control.

    The wrapper-generated ``config_toml`` is always written to
    ``<home>/.grok/config.toml``. ``sandbox_toml`` (Task 6's
    ``render_sandbox_toml`` output) is written to
    ``<home>/.grok/sandbox.toml`` only when not None; the file is not
    created at all when it is None.

    Any exception raised while building the home (after the auth-missing
    check has already passed), of any type, not just OSError, is logged
    with function/operation context, triggers a best-effort discard of the
    partially-built home (so a non-OSError raised after the auth copy step
    can never leave the copied authentication material on disk), and is
    then re-raised unchanged: this module does not invent an unlisted
    error class for a failure mode the Produces contract does not name.
    """
    missing_auth_file_names = [name for name in auth_file_names if not (source_grok_dir / name).is_file()]
    if missing_auth_file_names:
        _log_stderr(
            "create_private_home",
            "missing auth file(s) under {}: {}".format(source_grok_dir, missing_auth_file_names),
        )
        raise GrokWrapperError(
            "auth-missing",
            "missing authentication file(s) under source grok directory: {}".format(
                ", ".join(missing_auth_file_names)
            ),
            {"missingAuthFileNames": missing_auth_file_names},
        )

    home_dir = pathlib.Path(tempfile.mkdtemp(prefix=TEMP_HOME_PREFIX))
    platformsupport.restrict_dir_permissions(home_dir)
    grok_dir = home_dir / _GROK_DIR_NAME
    config_path = grok_dir / _CONFIG_TOML_FILENAME

    try:
        _mkdir_new_0700(grok_dir)

        # Stale-home audit reapability: stamp the C2 owner marker into the
        # private home the instant it exists, BEFORE any auth material is
        # copied in. runstate.audit_stale_temp_homes only reaps a gs-* home
        # whose owner.json verifies (owner string + valid run id); without this
        # marker a crash (SIGKILL) after the auth copy would strand a live
        # credential copy the audit could never reap. Reuses write_owner_marker
        # (which delegates to write_owner_marker_file) so the C2 marker schema
        # and the 0600 write live in exactly one place (DRY). The run id here is
        # a fresh valid id: the marker's job in a private home is to prove
        # ownership for the audit, not to correlate to a run record.
        write_owner_marker(home_dir, new_run_id())

        # Grok dogfood-3 #1: stamp the owning wrapper process pid into the home as
        # a liveness lease, BEFORE any auth material is copied in. The stale-home
        # reaper never removes a home whose owner pid is still alive, so a live run
        # with a long --timeout can never have its active credential-bearing home
        # reaped by age alone. Written here (not in runstate.create_run) because
        # the process that builds the home is the one that stays alive for the run.
        write_home_liveness_marker(home_dir, os.getpid())

        for name in auth_file_names:
            source_path = source_grok_dir / name
            dest_path = grok_dir / name
            _copy_auth_file_0600(source_path, dest_path)

        # D4(a): capture the exact injected credential value(s) from the COPIED
        # auth material into this run's redaction denylist, so the envelope builder
        # can mask any exact echo of them from stdout regardless of shape. Reads the
        # freshly-written copies under grok_dir (never the operator's source). This
        # ALWAYS sets the per-run denylist (to the extracted values, or empty when
        # nothing could be parsed), so it never carries a stale prior home's values.
        # Fail-safe: register_injected_secrets_from_home never raises.
        injectedsecrets.register_injected_secrets_from_home(grok_dir, auth_file_names)

        _write_text_0600(config_path, config_toml)

        if sandbox_toml is not None:
            _write_text_0600(grok_dir / _SANDBOX_TOML_FILENAME, sandbox_toml)
    except BaseException as exc:
        # Any exception, not just OSError: a non-OSError raised after the
        # auth copy loop (e.g. from a future post-copy step) must not leave
        # the copied authentication material on disk. Cleanup is
        # best-effort and never raises; the original exception is always
        # re-raised unchanged below.
        _log_stderr("create_private_home", "failed building private home {}: {}".format(home_dir, exc))
        _best_effort_discard_partial_home(home_dir)
        raise

    return PrivateHome(home_dir=home_dir, grok_dir=grok_dir, config_path=config_path)


def _remove_file_best_effort(path: pathlib.Path, function_name: str) -> bool:
    """Remove one file via ``os.remove``. Returns True on success or if it was already absent.

    Returns False only when a removal was genuinely attempted and failed
    for a reason other than the file already being gone, after logging the
    failure with function/operation context. Never raises.
    """
    try:
        os.remove(str(path))
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log_stderr(function_name, "failed to remove {}: {}".format(path, exc))
        return False


def destroy_private_home(home: "PrivateHome") -> "dict":
    """Tear down a private Grok HOME per C2 / step 3.

    Authentication material copies are removed FIRST, before any other
    cleanup step; every removal step is best-effort and logged, never
    raising. Returns ``{"status": "clean"|"failed", "detail": str|None}``.

    When any authentication material copy cannot be confirmed removed
    (including when the .grok directory itself cannot even be enumerated,
    which fails closed rather than silently reporting "clean"), status is
    "failed" with a fixed, path-free detail string: it never includes the
    home directory, the grok directory, or any file path, so a caller
    logging or displaying this detail cannot leak where the (possibly
    still-present) auth copy lives. Residual non-authentication material
    (the wrapper-generated config.toml/sandbox.toml, or anything the final
    ``shutil.rmtree`` cannot clear) also yields status "failed", but that
    detail is not subject to the path-free requirement.
    """
    # Auth material is every regular file ANYWHERE under .grok/ except the two
    # top-level wrapper files; a walk error fails closed (Grok r3 #13).
    auth_paths, auth_removal_failed = _enumerate_grok_auth_files(home.grok_dir)
    for auth_path in auth_paths:
        if not _remove_file_best_effort(auth_path, "destroy_private_home"):
            auth_removal_failed = True

    non_auth_removal_failed = False
    for wrapper_generated_name in sorted(_WRAPPER_GENERATED_GROK_FILENAMES):
        wrapper_generated_path = home.grok_dir / wrapper_generated_name
        if not _remove_file_best_effort(wrapper_generated_path, "destroy_private_home"):
            non_auth_removal_failed = True

    residual_paths: List[str] = []

    def _on_rmtree_error(_func: object, failed_path: str, _exc_info: object) -> None:
        residual_paths.append(failed_path)
        _log_stderr(
            "destroy_private_home",
            "failed to remove residual path during final rmtree: {}".format(failed_path),
        )

    shutil.rmtree(str(home.home_dir), onerror=_on_rmtree_error)
    if residual_paths:
        non_auth_removal_failed = True

    if auth_removal_failed:
        return {
            "status": "failed",
            "detail": "one or more authentication material copies could not be confirmed removed",
        }
    if non_auth_removal_failed:
        return {
            "status": "failed",
            "detail": "private grok home cleanup left residual non-authentication files",
        }
    return {"status": "clean", "detail": None}


def render_config_toml(*, mode: str) -> str:
    """Render the minimal, defense-in-depth ``config.toml`` written into every private Grok home.

    This file is write-only: groklib never parses it back (no ``tomllib``
    dependency; the Python 3.9 syntax floor has none). The C6 CLI flags
    (``--permission-mode auto``, ``--no-memory``) are the primary
    enforcement mechanism for every mode; this file's sole job is to make
    sure the private home's own config cannot silently reintroduce a
    blanket-approval permission stance or cross-session memory, in case a
    future Grok CLI release ever reads config.toml ahead of, or instead of,
    those CLI flags. Task 0's probe report pinned ``HEADLESS_PERMISSION_MODE``
    to a single value, "auto", for every mode (probe-report.md, Steps 4-5:
    "Pinned: HEADLESS_PERMISSION_MODE = auto"), so the rendered
    ``permission_mode`` value is the same for every mode; ``mode`` is still
    required and validated (fail closed on empty/non-string input) so the
    generated file documents, in a comment, which mode it was generated
    for.

    ``mode`` is additionally required to fully match ``^[a-z]+$`` before it
    is interpolated into the generated comment: this is checked with
    ``re.fullmatch`` ahead of any string formatting, so a mode value
    carrying a newline (or any other character outside ``a-z``) can never
    be used to inject an extra line into the rendered TOML. On mismatch the
    rejected value is never interpolated into the raised exception's
    message unsanitized; only a ``repr()`` of it, truncated to 40
    characters, appears there.
    """
    if not isinstance(mode, str) or not mode:
        _log_stderr("render_config_toml", "rejected empty or non-string mode {!r}".format(mode))
        raise GrokWrapperError(
            "usage-error",
            "render_config_toml requires a non-empty mode string",
            {"mode": mode},
        )

    if re.fullmatch(r"[a-z]+", mode) is None:
        rejected_mode_repr = repr(mode)[:40]
        _log_stderr(
            "render_config_toml",
            "rejected mode not matching ^[a-z]+$: {}".format(rejected_mode_repr),
        )
        raise ValueError(
            "render_config_toml rejected mode not matching ^[a-z]+$: {}".format(rejected_mode_repr)
        )

    return (
        "# Generated by groklib.authhome.render_config_toml for mode \"{mode}\".\n"
        "# Defense-in-depth only: the C6 CLI flags (--permission-mode auto,\n"
        "# --no-memory) are the primary enforcement for every mode. This file\n"
        "# exists solely so the private home's own config cannot silently\n"
        "# grant a blanket-approval permission stance or re-enable\n"
        "# cross-session memory. Write-only: groklib never parses this file\n"
        "# back (no tomllib dependency; Python 3.9 syntax floor).\n"
        "\n"
        "[permissions]\n"
        "permission_mode = \"auto\"\n"
        "\n"
        "[memory]\n"
        "enabled = false\n"
    ).format(mode=mode)
