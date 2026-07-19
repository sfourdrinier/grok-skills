# wrapper/scripts/groklib/sandbox.py
#
# C4/C6 sandbox policy and enforcement verification. Per user decision
# D-SECRETREAD (2026-07-14, tests/fixtures/sandbox-custom-probe.md), the
# Task 6 Step 5 live probe proved grok 0.2.101 cannot deny credential/secret
# reads through any sandbox.toml schema; the user accepted this read gap and
# withdrew the secret-read-denial requirement. This module's remaining and
# STILL fail-closed security job is WRITE confinement. It owns three
# responsibilities:
#
#   1. policy_for_mode: resolve the mode's base built-in sandbox profile
#      (Task 0 pins) plus the legitimate writable roots. It NEVER raises
#      probe-required: it always returns a valid write-confinement
#      SandboxPolicy for every known mode. secret_read_denial_proven is
#      recorded honestly as False (the gap is accepted, not proven closed)
#      and is purely INFORMATIONAL; it never gates this function or any
#      other function in this module.
#
#   2. render_sandbox_toml: author a custom ~/.grok/sandbox.toml profile
#      stanza (written into the private home by authhome) that extends the
#      base built-in and MAY list the operator's real-home credential
#      directories under deny_read_globs as a best-effort, defense-in-depth
#      artifact. deny_read_globs is a real config key confirmed in the grok
#      0.2.101 binary, BUT the Task 6 Step 5 live probe proved it does NOT
#      actually deny reads on grok 0.2.101 (macos/seatbelt): reads are
#      unconditionally permitted; only writes are confined. See
#      tests/fixtures/sandbox-custom-probe.md. This function therefore emits
#      an honest best-effort artifact, explicitly commented as unenforced,
#      and must never be relied on for read denial. No network restriction
#      is added: child-process network egress is permitted per user
#      decision D-NET (2026-07-14).
#
#   3. verify_enforcement: read <home>/.grok/sandbox-events.jsonl (the C4
#      sandbox.evidence source pinned by Task 0 Step 5e, NOT the stdout
#      envelope), confirm a ProfileApplied event exists with the expected
#      profile and enforced=true, and build the C4 sandbox sub-object. This
#      STILL fails closed on absent evidence, a missing/mismatched
#      ProfileApplied, enforced not true, or a write FsViolation that
#      succeeded outside the run's legitimate writable roots. It does NOT
#      assert or require read denial.
#
# WRITE confinement is the fail-closed boundary this module still enforces:
# missing, malformed, ambiguous, or unverifiable input becomes a classified
# GrokWrapperError, never a silent default. No file contents are logged;
# only structural facts (event types, profile names, path targets) appear
# in diagnostics.

import dataclasses
import json
import os
import pathlib
from typing import Dict, List, Tuple

from groklib import GrokWrapperError, log_stderr
from groklib import platformsupport
from groklib.authhome import PrivateHome

# Task 0 pin (probe-report.md, "Pinned constants" and Step 5): the base
# built-in sandbox profile per mode. review/reason confine writes only
# (read-only); code/verify confine writes to cwd plus private temp
# (workspace). policy_for_mode returns a DISTINCT custom profile name
# (custom_profile_name: "grok-skills-<mode>") that EXTENDS this base built-in, so
# the rendered sandbox.toml never shadows/redefines the built-in (Grok
# dogfood-2 #6); this table is the base each custom profile extends.
SANDBOX_PROFILE_BY_MODE: Dict[str, str] = {
    "review": "read-only",
    "reason": "read-only",
    "code": "workspace",
    "verify": "workspace",
    # Default-on ACP peer channel (start parity with code; opt out GROK_DISABLE_ACP=1).
    "peer": "workspace",
    # Hardened-direct: Grok edits the operator repo root (no worktree).
    "direct": "workspace",
}

# The modes whose legitimate writable roots include an external worktree.
# review/reason never write outside the private Grok session state supplied
# by the base profile, so their writable_roots are empty.
# direct is intentionally NOT here: its writable root is the repo root itself.
_WORKTREE_MODES = frozenset({"code", "verify", "peer"})

# Modes that confine writes to the operator repository root (+ private tmp)
# rather than an external worktree. direct is the sole member today.
_REPO_ROOT_WRITE_MODES = frozenset({"direct"})

# The custom sandbox profile is given a DISTINCT name (never a built-in profile
# name) so the rendered sandbox.toml stanza EXTENDS the built-in write-
# confinement profile instead of shadowing/redefining it (Grok dogfood-2 #6). A
# stanza named for the built-in (e.g. `[profiles.read-only] extends = "read-only"`)
# would self-extend and shadow the built-in, so grok would apply an UNVALIDATED
# custom profile rather than the built-in the Task 0 / Task 6 live probes proved
# enforced. The probes ("grok-skills-probe extends workspace") and the sandbox unit
# tests ("grok-skills-review"/"grok-skills-code") use exactly this "grok-skills-<mode>"
# naming; policy_for_mode now returns it so the runtime matches what was probed.
_CUSTOM_PROFILE_PREFIX = "grok-skills-"


def custom_profile_name(mode: str) -> str:
    """The distinct custom sandbox profile name for ``mode`` (never a built-in name)."""
    return _CUSTOM_PROFILE_PREFIX + mode

# SECRET-READ DENIAL: INFORMATIONAL ONLY, NEVER A GATE (user decision
# D-SECRETREAD, 2026-07-14). The Task 6 Step 5 live custom-profile probe
# proved grok 0.2.101 (macos/seatbelt) does not enforce read denial through
# ANY sandbox.toml key (deny_read_globs, deny_paths, and even a narrow
# read_only allowlist all left planted sentinels readable; see
# tests/fixtures/sandbox-custom-probe.md). The user accepted this read gap
# and withdrew the secret-read-denial requirement, so this table stays False
# for every mode. policy_for_mode no longer gates on it: the value is
# surfaced on SandboxPolicy.secret_read_denial_proven purely for operator
# visibility. WRITE confinement (the built-in "read-only"/"workspace"
# profiles) remains fully enforced and is the sole security boundary
# verify_enforcement checks.
# peer shares code's workspace profile; secret-read denial is still unproven.
SECRET_READ_DENIAL_PROVEN_BY_MODE: Dict[str, bool] = {
    "review": False,
    "reason": False,
    "code": False,
    "verify": False,
    "peer": False,
    "direct": False,
}

# Real-home credential directories listed for the Grok child to be denied.
# The child runs with HOME set to its private home, so it never legitimately
# needs to read the operator's real-home credential material at these
# absolute paths. deny_read_globs (binary schema evidence: `deny_read_globs`,
# `deny_read_globs_from_config`) is the intended read-denial key, but the
# Step 5 probe proved it is not enforced on grok 0.2.101 (D-SECRETREAD);
# render_sandbox_toml still emits it as a best-effort, defense-in-depth
# artifact, explicitly commented as unenforced. The per-platform credential
# directory set (macOS keychains; Linux .gnupg/.config; Windows credential
# store dirs) lives in platformsupport.credential_deny_dirs (D-PORT), so this
# module never hardcodes a macOS-specific keychain path.

_SANDBOX_EVENTS_FILENAME = "sandbox-events.jsonl"
_EVENT_TYPE_KEY = "event_type"
_PROFILE_APPLIED_EVENT = "ProfileApplied"
_FS_VIOLATION_EVENT = "FsViolation"


def _log(function: str, message: str) -> None:
    """Delegate to the shared groklib.log_stderr, pre-binding the "sandbox" component prefix."""
    log_stderr("sandbox", function, message)


@dataclasses.dataclass(frozen=True)
class SandboxPolicy:
    """Resolved sandbox decision for one run.

    ``profile`` is the base built-in profile passed to Grok's ``--sandbox``
    flag (``read-only`` for review/reason, ``workspace`` for code/verify).
    ``writable_roots`` are the absolute paths the run may legitimately write
    (external worktree and private temp for code/verify; empty for
    review/reason). ``secret_read_denial_proven`` is always False (user
    decision D-SECRETREAD: the read gap is accepted, not proven closed) and
    is purely INFORMATIONAL -- it is recorded for operator visibility and
    never gates ``policy_for_mode`` or any other function in this module.
    """

    mode: str
    profile: str
    writable_roots: "tuple[str, ...]"
    secret_read_denial_proven: bool


def _require_absolute_dir_arg(function: str, name: str, value: object) -> pathlib.Path:
    """Require a pathlib.Path argument that is already absolute, failing closed otherwise.

    The absoluteness check happens BEFORE any caller calls ``.resolve()`` so a
    relative path is never silently resolved against the current working
    directory (Task 6 M1). The function name now matches its behavior: it
    rejects both a non-Path and a relative Path as a usage error.
    """
    if not isinstance(value, pathlib.Path):
        _log(function, "rejected non-Path {} argument of type {}".format(name, type(value).__name__))
        raise GrokWrapperError(
            "usage-error",
            "{} requires {} to be a pathlib.Path".format(function, name),
            {"argument": name},
        )
    if not value.is_absolute():
        _log(function, "rejected relative {} argument".format(name))
        raise GrokWrapperError(
            "usage-error",
            "{} requires {} to be an absolute path".format(function, name),
            {"argument": name},
        )
    return value


def policy_for_mode(
    mode: str,
    *,
    worktree: "pathlib.Path|None",
    private_tmp: pathlib.Path,
    repo_root: "pathlib.Path|None" = None,
) -> SandboxPolicy:
    """Resolve the write-confinement SandboxPolicy for ``mode``.

    Per user decision D-SECRETREAD (2026-07-14): the Task 6 Step 5 live
    probe proved grok 0.2.101 cannot deny credential/secret reads through
    any sandbox.toml schema (evidence: tests/fixtures/sandbox-custom-probe.md).
    The user accepted this read gap, withdrawing the secret-read-denial
    requirement. This function therefore NEVER raises ``probe-required``: it
    always returns a valid SandboxPolicy for every known mode.

    Order of checks (each fails closed before the next):
      1. ``mode`` must be a known mode; an unknown mode is a usage error.
      2. ``private_tmp`` must be a pathlib.Path.
      3. For direct, ``repo_root`` must be an absolute directory (writable root).
      4. For code/verify/peer the ``worktree`` must be provided; there are no
         legitimate writable roots for a worktree write-capable mode without it,
         so a missing worktree is a usage error.

    Returns a SandboxPolicy for every valid mode: review/reason resolve to the
    custom ``grok-skills-review``/``grok-skills-reason`` profile (extending the base
    ``read-only`` built-in) with no writable roots (they never write); direct
    resolves to ``grok-skills-direct`` (extending workspace) with the repo root and
    private tmp; code/verify/peer resolve to the custom ``grok-skills-<mode>``
    profile (extending the base ``workspace`` built-in) with the resolved worktree
    and private tmp as the legitimate writable roots. The distinct name avoids
    shadowing the built-in (Grok dogfood-2 #6).
    ``secret_read_denial_proven`` is recorded honestly as False for every
    mode (the read gap is accepted, never proven closed) and is purely
    INFORMATIONAL: it never gates this function's success. Network egress is
    never a gate either (user decision D-NET).
    """
    if not isinstance(mode, str) or mode not in SANDBOX_PROFILE_BY_MODE:
        _log("policy_for_mode", "rejected unknown mode {!r}".format(mode))
        raise GrokWrapperError(
            "usage-error",
            "unknown sandbox mode: {!r}".format(mode),
            {"mode": mode},
        )

    private_tmp = _require_absolute_dir_arg("policy_for_mode", "private_tmp", private_tmp)

    if mode in _REPO_ROOT_WRITE_MODES:
        if repo_root is None:
            _log("policy_for_mode", "mode {} requires a repo_root but none was provided".format(mode))
            raise GrokWrapperError(
                "usage-error",
                "sandbox mode {} requires a repo_root".format(mode),
                {"mode": mode},
            )
        repo_root = _require_absolute_dir_arg("policy_for_mode", "repo_root", repo_root)
        writable_roots: Tuple[str, ...] = (
            str(repo_root.resolve()),
            str(private_tmp.resolve()),
        )
    elif mode in _WORKTREE_MODES:
        if worktree is None:
            _log("policy_for_mode", "mode {} requires a worktree but none was provided".format(mode))
            raise GrokWrapperError(
                "usage-error",
                "sandbox mode {} requires a worktree".format(mode),
                {"mode": mode},
            )
        worktree = _require_absolute_dir_arg("policy_for_mode", "worktree", worktree)
        writable_roots = (
            str(worktree.resolve()),
            str(private_tmp.resolve()),
        )
    else:
        writable_roots = ()

    return SandboxPolicy(
        mode=mode,
        # A DISTINCT custom profile name that EXTENDS the mode's base built-in
        # (Grok dogfood-2 #6), never the built-in name itself -- so the rendered
        # sandbox.toml stanza and the --sandbox flag reference a profile that
        # augments, rather than shadows, the built-in write-confinement profile.
        profile=custom_profile_name(mode),
        writable_roots=writable_roots,
        secret_read_denial_proven=SECRET_READ_DENIAL_PROVEN_BY_MODE[mode],
    )


def _escape_toml_basic_string(function: str, value: str) -> str:
    """Escape a value for a TOML basic string, failing closed on control characters.

    Real-home paths never legitimately contain a newline; rejecting one
    prevents a maliciously named home directory from injecting an extra TOML
    line into the generated profile.
    """
    if "\n" in value or "\r" in value or "\x00" in value:
        _log("_escape_toml_basic_string", "rejected path containing a control character")
        raise GrokWrapperError(
            "usage-error",
            "refusing to render a sandbox path containing a control character",
            {"reason": "control-character-in-path"},
        )
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _credential_deny_globs(real_home: pathlib.Path) -> List[str]:
    """Build the deny_read_globs list for the operator's real-home credential dirs.

    The per-platform credential directory set (macOS keychains; Linux
    .gnupg/.config; Windows credential store dirs) comes from
    ``platformsupport.credential_deny_dirs`` (D-PORT), so this module holds no
    macOS-specific keychain path. Each directory is denied both as the bare
    path and as ``<dir>/**`` so a read of the directory entry itself and of
    any file beneath it is denied. These globs are best-effort, defense-in-
    depth only (D-SECRETREAD): grok 0.2.101 does not enforce them.
    """
    globs: List[str] = []
    for absolute_dir in platformsupport.credential_deny_dirs(real_home):
        globs.append(absolute_dir)
        globs.append(absolute_dir + "/**")
    return globs


def render_sandbox_toml(policy: SandboxPolicy, *, real_home: pathlib.Path) -> str:
    """Render a custom ~/.grok/sandbox.toml profile stanza extending the mode's base built-in.

    ``policy.profile`` names the rendered stanza (``[profiles.<name>]``);
    ``policy.mode`` selects the base built-in it extends via
    SANDBOX_PROFILE_BY_MODE. The stanza keeps the write-confinement
    ``extends`` relationship and lists the operator's real-home credential
    directories under ``deny_read_globs`` as a best-effort, defense-in-depth
    artifact: per user decision D-SECRETREAD (2026-07-14,
    tests/fixtures/sandbox-custom-probe.md), grok 0.2.101 does NOT enforce
    read denial through this or any other sandbox.toml key, so the wrapper
    must never rely on it -- write confinement (the base built-in profile)
    is the only boundary this module enforces. No network restriction is
    added: child-process network egress is permitted (user decision D-NET,
    2026-07-14). This file is write-only; groklib never parses it back.

    Fails closed if ``policy.mode`` is unknown or ``real_home`` is not an
    absolute path.
    """
    real_home = _require_absolute_dir_arg("render_sandbox_toml", "real_home", real_home)

    base_profile = SANDBOX_PROFILE_BY_MODE.get(policy.mode)
    if base_profile is None:
        _log("render_sandbox_toml", "rejected unknown mode {!r}".format(policy.mode))
        raise GrokWrapperError(
            "usage-error",
            "render_sandbox_toml received an unknown mode: {!r}".format(policy.mode),
            {"mode": policy.mode},
        )

    deny_glob_lines = "".join(
        '  "{}",\n'.format(_escape_toml_basic_string("render_sandbox_toml", glob))
        for glob in _credential_deny_globs(real_home)
    )

    return (
        "# Generated by groklib.sandbox.render_sandbox_toml for mode \"{mode}\".\n"
        "# Custom sandbox profile: extends the base built-in \"{base}\" and lists\n"
        "# real-home credential dirs under deny_read_globs as a best-effort,\n"
        "# defense-in-depth artifact. Per user decision D-SECRETREAD\n"
        "# (2026-07-14), grok 0.2.101 does NOT enforce read denial via this or\n"
        "# any sandbox.toml key (tests/fixtures/sandbox-custom-probe.md); these\n"
        "# globs must NOT be relied on. Write confinement (the base built-in\n"
        "# profile) is the only boundary this wrapper enforces. Child-process\n"
        "# network egress is intentionally NOT restricted (user decision\n"
        "# D-NET, 2026-07-14). Write-only: groklib never parses this file back.\n"
        "[profiles.{profile}]\n"
        "extends = \"{base}\"\n"
        "deny_read_globs = [\n"
        "{deny_glob_lines}"
        "]\n"
    ).format(
        mode=_escape_toml_basic_string("render_sandbox_toml", policy.mode),
        base=base_profile,
        profile=policy.profile,
        deny_glob_lines=deny_glob_lines,
    )


def _read_sandbox_events(function: str, events_path: pathlib.Path) -> List["dict"]:
    """Read and JSON-decode each line of the sandbox events file, failing closed if it is absent.

    A trailing partial line (torn concurrent write) and any individually
    malformed line are skipped with a stderr warning, matching the C3
    progress-stream reader discipline. This does not weaken enforcement: the
    caller still REQUIRES a valid ProfileApplied event, so if the applied
    event itself is unreadable the verification fails closed downstream.
    """
    try:
        with open(str(events_path), "r", encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except FileNotFoundError:
        _log(function, "sandbox events file absent: {}".format(events_path))
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox enforcement evidence is missing (no sandbox-events.jsonl)",
            {"eventsFileName": _SANDBOX_EVENTS_FILENAME},
        )
    except OSError as exc:
        _log(function, "failed to read sandbox events file {}: {}".format(events_path, exc))
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox enforcement evidence could not be read",
            {"eventsFileName": _SANDBOX_EVENTS_FILENAME},
        )

    events: List["dict"] = []
    for line_number, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            _log(function, "skipping malformed sandbox event on line {}: {}".format(line_number, exc))
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
        else:
            _log(function, "skipping non-object sandbox event on line {}".format(line_number))
    return events


def _path_within_expected_roots(target: str, expected_roots: "tuple[str, ...]") -> bool:
    """Return True if ``target`` resolves inside any expected root, symlinks resolved.

    Uses ``os.path.realpath`` on both sides so the macOS ``/var`` -> ``/private/var``
    and ``/tmp`` -> ``/private/tmp`` symlinks map a telemetry path such as
    ``/var/folders/.../.grok`` onto the ``/private/var/folders`` session-temp
    root. Used by BOTH the M2 write-allowlist subset check AND the FsViolation
    write-confinement guard (PR968 codex fsviolation-resolve): policy
    ``writable_roots`` are stored after ``.resolve()`` (``/private/var/...``)
    while sandbox telemetry may report the denial under the equivalent
    ``/var/...`` spelling, so a normpath-only compare would miss that a BLOCKED
    required write fell INSIDE the legitimate writable root.
    """
    normalized_target = os.path.realpath(target)
    for root in expected_roots:
        normalized_root = os.path.realpath(root)
        if normalized_target == normalized_root:
            return True
        if normalized_target.startswith(normalized_root + os.sep):
            return True
    return False


def verify_enforcement(home: PrivateHome, policy: SandboxPolicy) -> "dict":
    """Read <home>/.grok/sandbox-events.jsonl and build the C4 sandbox sub-object.

    Fails closed with GrokWrapperError("probe-required") FIRST when this host
    has no captured Grok sandbox probe report (per-platform SECURITY GUARANTEE,
    D-PORT): macOS is the only probed platform in v1, so Linux/Windows live
    verification stays blocked until their own probe suite runs. The probed
    platform's expected ProfileApplied.platform label
    (platformsupport.expected_sandbox_platform) is compared against the
    telemetry instead of hardcoding ``macos/seatbelt``.

    Raises GrokWrapperError("sandbox-failure") when:
      - the events file is absent or unreadable,
      - no ProfileApplied event is present,
      - the applied profile does not match ``policy.profile``,
      - the applied profile is not reported enforced,
      - the applied platform does not match the probed platform's expected
        sandbox platform label,
      - an FsViolation blocked a write INSIDE a legitimate writable root
        (the applied profile contradicts the intended policy),
      - the applied read_write_paths granted a write OUTSIDE the policy's
        writable_roots plus the platform's mandatory session-temp roots
        (Task 6 M2).

    FsViolation events whose targets fall OUTSIDE the writable roots are the
    sandbox correctly denying an escape; they are recorded as evidence, not
    treated as failures.

    This function verifies WRITE confinement only (D-SECRETREAD withdrew the
    secret-read-denial requirement); it never asserts or requires read
    denial.

    Note on the writable-roots guard: the Task 0 FsViolation event records a
    write the sandbox DENIED (its presence proves active denial, never a
    successful escape, which seatbelt does not log). The only self-consistent
    FsViolation-based failure detectable from this schema is a denial of a
    path the run was entitled to write, so that is the condition checked
    here. This is a fail-closed strengthening; it never suppresses a real
    denial.
    """
    # Per-platform SECURITY GUARANTEE (D-PORT): fail closed on any platform
    # without a captured Grok sandbox probe report, before touching evidence.
    platformsupport.require_probed_platform_for_live()

    if not isinstance(home, PrivateHome):
        _log("verify_enforcement", "rejected non-PrivateHome home argument")
        raise GrokWrapperError(
            "sandbox-failure",
            "verify_enforcement requires a PrivateHome",
            {"argument": "home"},
        )
    if not isinstance(policy, SandboxPolicy):
        _log("verify_enforcement", "rejected non-SandboxPolicy policy argument")
        raise GrokWrapperError(
            "sandbox-failure",
            "verify_enforcement requires a SandboxPolicy",
            {"argument": "policy"},
        )

    events_path = home.grok_dir / _SANDBOX_EVENTS_FILENAME
    events = _read_sandbox_events("verify_enforcement", events_path)

    profile_applied_events = [
        event for event in events if event.get(_EVENT_TYPE_KEY) == _PROFILE_APPLIED_EVENT
    ]
    if not profile_applied_events:
        _log("verify_enforcement", "no ProfileApplied event in {}".format(events_path))
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox evidence contains no ProfileApplied event",
            {"eventsFileName": _SANDBOX_EVENTS_FILENAME},
        )

    # The last ProfileApplied event is the profile in force at run time.
    applied = profile_applied_events[-1]
    reported_profile = applied.get("profile")
    if reported_profile != policy.profile:
        _log(
            "verify_enforcement",
            "profile mismatch: requested {!r} reported {!r}".format(policy.profile, reported_profile),
        )
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox profile mismatch: requested {!r} but telemetry reported {!r}".format(
                policy.profile, reported_profile
            ),
            {"requestedProfile": policy.profile, "reportedProfile": reported_profile},
        )

    enforced = applied.get("enforced")
    if enforced is not True:
        _log("verify_enforcement", "profile {!r} reported enforced={!r}".format(reported_profile, enforced))
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox profile {!r} is not reported as enforced".format(reported_profile),
            {"reportedProfile": reported_profile, "enforced": enforced},
        )

    # Compare the telemetry platform against the probed platform's expected
    # sandbox label (D-PORT) instead of hardcoding "macos/seatbelt".
    expected_platform = platformsupport.expected_sandbox_platform()
    reported_platform = applied.get("platform")
    if reported_platform != expected_platform:
        _log(
            "verify_enforcement",
            "platform mismatch: expected {!r} reported {!r}".format(expected_platform, reported_platform),
        )
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox platform mismatch: expected {!r} but telemetry reported {!r}".format(
                expected_platform, reported_platform
            ),
            {"expectedPlatform": expected_platform, "reportedPlatform": reported_platform},
        )

    fs_violations = [event for event in events if event.get(_EVENT_TYPE_KEY) == _FS_VIOLATION_EVENT]
    violation_summaries: List[str] = []
    for violation in fs_violations:
        target = violation.get("target")
        operation = violation.get("operation")
        if isinstance(target, str):
            violation_summaries.append("{}:{}".format(operation, target))
            if _path_within_expected_roots(target, policy.writable_roots):
                _log(
                    "verify_enforcement",
                    "FsViolation blocked a write inside a writable root: {}".format(target),
                )
                raise GrokWrapperError(
                    "sandbox-failure",
                    "sandbox blocked a {} inside a legitimate writable root".format(operation),
                    {"target": target, "operation": operation},
                )
        else:
            violation_summaries.append("{}:<no-target>".format(operation))

    restrict_network = applied.get("restrict_network")
    read_write_paths = applied.get("read_write_paths")

    # S1/SEC3: for a WRITE-CAPABLE (workspace) profile -- one whose policy grants
    # legitimate writable roots (code/verify) -- the ProfileApplied event MUST
    # enumerate the granted read_write_paths as a list so write confinement can
    # actually be verified. An absent or non-list read_write_paths there is
    # unverifiable: fail closed with sandbox-failure rather than silently skip
    # the subset check and report read_write_paths=0. Read-only modes
    # (review/reason) have no writable roots to confine, so an absent list is
    # acceptable for them.
    if policy.writable_roots and not isinstance(read_write_paths, list):
        _log(
            "verify_enforcement",
            "write-capable profile {!r} reported no read_write_paths list (type {})".format(
                reported_profile, type(read_write_paths).__name__
            ),
        )
        raise GrokWrapperError(
            "sandbox-failure",
            "sandbox evidence for a write-capable profile omitted the granted "
            "read_write_paths; write confinement is unverifiable",
            {
                "reportedProfile": reported_profile,
                "readWritePathsType": type(read_write_paths).__name__,
            },
        )

    # Task 6 M2: every write path the profile actually granted must fall
    # inside the policy's writable_roots plus the platform's mandatory
    # session-temp roots. A grant to any other location means the applied
    # profile is wider than the intended write confinement, so fail closed.
    expected_write_roots = tuple(policy.writable_roots) + platformsupport.mandatory_session_temp_roots()
    if isinstance(read_write_paths, list):
        for granted_path in read_write_paths:
            granted_ok = isinstance(granted_path, str) and _path_within_expected_roots(
                granted_path, expected_write_roots
            )
            if not granted_ok:
                _log(
                    "verify_enforcement",
                    "applied read_write path outside expected roots: {!r}".format(granted_path),
                )
                raise GrokWrapperError(
                    "sandbox-failure",
                    "sandbox granted a write path outside the run's legitimate "
                    "writable and session-temp roots",
                    {"grantedPath": granted_path if isinstance(granted_path, str) else None},
                )

    # Grok r5 #3: the subset check above passes VACUOUSLY for an EMPTY
    # read_write_paths (or one that omits a writable root), yet an empty/partial
    # grant proves NOTHING about write confinement -- reporting enforced=true with
    # read_write_paths=0 while the real profile may still allow cwd writes. For a
    # write-capable profile, require every one of the run's legitimate writable roots
    # (the external worktree and its private tmp) to be COVERED by some granted path
    # (equal to it or an ancestor of it); an empty or missing-expected grant fails
    # closed. This does NOT tighten the broad session-temp acceptance (deferred D3);
    # it only requires the expected grant to be PRESENT.
    if policy.writable_roots:
        granted_str_paths = tuple(p for p in read_write_paths if isinstance(p, str)) if isinstance(
            read_write_paths, list
        ) else ()
        for expected_root in policy.writable_roots:
            if not _path_within_expected_roots(expected_root, granted_str_paths):
                _log(
                    "verify_enforcement",
                    "write-capable profile {!r} did not grant the expected writable root {!r}".format(
                        reported_profile, expected_root
                    ),
                )
                raise GrokWrapperError(
                    "sandbox-failure",
                    "sandbox evidence for a write-capable profile did not grant write "
                    "access to a legitimate writable root; write confinement is unverifiable",
                    {"reportedProfile": reported_profile, "missingWritableRoot": expected_root},
                )

    read_write_count = len(read_write_paths) if isinstance(read_write_paths, list) else 0

    evidence = (
        "platform={platform} enforced=true restrict_network={restrict_network} "
        "read_write_paths={read_write_count} fs_violations={violation_count}"
    ).format(
        platform=reported_platform,
        restrict_network=restrict_network,
        read_write_count=read_write_count,
        violation_count=len(fs_violations),
    )
    if violation_summaries:
        evidence = evidence + " [" + "; ".join(violation_summaries) + "]"

    return {
        "requestedProfile": policy.profile,
        "reportedProfile": reported_profile,
        "enforced": True,
        "evidence": evidence,
    }
