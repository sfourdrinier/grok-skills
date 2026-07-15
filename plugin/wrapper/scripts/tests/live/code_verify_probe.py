# wrapper/scripts/tests/live/code_verify_probe.py
#
# Task 14 write-capable code + verify handoff probe (spec 14.3). Split out of
# live_probes.py so neither file crosses the 900-line cap. This probe NEVER runs
# against the host checkout: it scaffolds a DISPOSABLE temp git repo (with a copy of
# the wrapper tooling inside it so the wrapper's git-toplevel repo-root
# resolution targets the throwaway repo), runs the real write-capable `code`
# there under an isolated XDG_STATE_HOME, then runs `verify` over the SAME
# worktree -- the real code->verify handoff, where the worktree still carries the
# code run's UNCOMMITTED edits. The retained (dirty) worktree is force-removed in
# a finally. It imports only from _probe_common (acyclic). Python stdlib only;
# 3.9 syntax. No em/en dashes. No empty excepts.

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from _probe_common import (
    ProbeError,
    ProbeResult,
    _ACCEPTED_VERSION_FILE,
    _SCRIPTS_DIR,
    _WRAPPER,
    _envelope_highlights,
    _read_progress_phases,
    _require,
)

# The write-capable code run authors files and runs tests live, so it gets a
# wider ceiling than the read-only probes; verify (read-only over the same
# worktree) reuses it.
_CODE_PROBE_TIMEOUT_SECONDS = 480

# Build/test/cache artifact directory names a verify run may legitimately touch
# (mirrors modes/verify._ARTIFACT_DIRS); used to prove verify flagged no SOURCE
# edit in the handoff probe.
_ARTIFACT_DIR_NAMES = ("node_modules", "dist", ".turbo", "coverage", "build", ".cache")

# Deterministic pnpm stand-in for the code build gate. The wrapper resolves it
# through GROK_PACKAGE_MANAGER_BINARY (its documented test/operator override), so the
# mandatory build gate records a real invocation with exit 0 without depending
# on a full pnpm workspace inside a throwaway repo. The LIVE surface -- Grok
# authoring files under sandbox write-confinement in an isolated worktree, the
# cwd sentinel, diff confinement, and the code->verify handoff -- is unaffected.
_PNPM_STUB_SCRIPT = (
    "#!/bin/sh\n"
    "# Deterministic pnpm stand-in (GROK_PACKAGE_MANAGER_BINARY) for the code build gate.\n"
    'case "$1" in\n'
    "  install) exit 0 ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)

_CODE_PROBE_TASK = (
    "Add a new file pkg/slugify.py defining a function slugify(value) that converts a string to a "
    "URL-safe slug: lowercase it, collapse every run of non-alphanumeric characters into a single "
    "hyphen, and strip leading and trailing hyphens. Also add pkg/test_slugify.py with a few "
    "unittest test cases covering plain words, spaces, punctuation, and leading/trailing separators. "
    "Then run the tests with `python3 -m unittest discover -s pkg -p 'test_*.py'` and confirm they "
    "pass. Keep every change inside the pkg directory."
)

_VERIFY_PROBE_TASK = (
    "Verify the slugify change in pkg. Read pkg/slugify.py and pkg/test_slugify.py, then run the "
    "test suite with `python3 -m unittest discover -s pkg -p 'test_*.py'`. Base your verdict on "
    "whether the tests pass and the implementation matches the described behavior. Do not edit any "
    "files."
)


def _run_wrapper_at(
    wrapper_path: pathlib.Path, mode_args: List[str], env_extra: Dict[str, str], timeout: int
) -> Tuple[int, Dict[str, object], str, float]:
    """Run a COPIED wrapper (grok_agent.py inside a disposable repo) with extra env; return (exit, envelope, stderr, seconds).

    The write-capable code run must resolve its repo root to the throwaway repo,
    never the host checkout, and the wrapper anchors that resolution on its own
    on-disk location -- so the code/verify probes run a copy placed inside the
    disposable repo. GROK_AGENT_BINARY is removed so the real ~/.grok/bin/grok is
    used; ``env_extra`` supplies the isolated XDG_STATE_HOME and the pnpm
    stand-in. stdout must be exactly one JSON envelope.
    """
    env = dict(os.environ)
    env.pop("GROK_AGENT_BINARY", None)
    env.update(env_extra)
    argv = [sys.executable, str(wrapper_path)] + mode_args
    start = time.monotonic()
    completed = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
        check=False,
    )
    duration = time.monotonic() - start
    stdout = completed.stdout or ""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(
            "wrapper stdout was not a single JSON envelope: {}; stderr tail: {}".format(
                exc, "\n".join((completed.stderr or "").splitlines()[-5:])
            )
        )
    if not isinstance(envelope, dict):
        raise ProbeError("wrapper stdout JSON was not an object")
    return completed.returncode, envelope, completed.stderr or "", duration


def _git_plain(repo: pathlib.Path, *args: str) -> None:
    """Run a git command in ``repo`` (argv list, never shell) that MUST succeed."""
    subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _repo_status(repo: pathlib.Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return completed.stdout or ""


def _path_is_within(child: pathlib.Path, root: pathlib.Path) -> bool:
    """True when ``child`` is ``root`` or nested under it (realpath-normalized)."""
    resolved_child = child.resolve()
    resolved_root = root.resolve()
    return resolved_child == resolved_root or resolved_root in resolved_child.parents


def _is_artifact_path(relative: str) -> bool:
    """True when the worktree-relative path's first component is a tolerated artifact directory."""
    head = pathlib.PurePosixPath(relative).parts[0] if relative else ""
    return head in _ARTIFACT_DIR_NAMES


def _scaffold_disposable_repo(root: pathlib.Path) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Build a disposable git repo carrying a wrapper copy + a minimal pkg workspace; return (repo, wrapper, pnpm-stub).

    The wrapper tooling is copied INSIDE the repo so its git-toplevel repo-root
    resolution targets this throwaway repo, never the host checkout (spec 14.3). The
    copy REPLICATES the real skill layout (<skill>/scripts/groklib + the sibling
    accepted-version.json) so the wrapper's own parents[2] pin-file and skill
    resolutions work unchanged. The committed pkg workspace has a package.json
    whose build script the code build gate runs (through the pnpm stand-in).
    """
    repo = root / "disposable-repo"
    repo.mkdir()
    _git_plain(repo, "init", "-q")
    _git_plain(repo, "config", "user.name", "Grok CLI Probe")
    _git_plain(repo, "config", "user.email", "grok-cli-probe@example.com")
    _git_plain(repo, "config", "commit.gpgsign", "false")

    skill_root = repo / "grok-skill"
    scripts_dir = skill_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(str(_WRAPPER), str(scripts_dir / "grok_agent.py"))
    shutil.copytree(
        str(_SCRIPTS_DIR / "groklib"),
        str(scripts_dir / "groklib"),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # The C6 pin file lives at <skill root>/accepted-version.json (the wrapper
    # resolves it as parents[2] of grokcli.py); copy the real, live-validated pin
    # so check_version passes against the installed binary exactly as in place.
    shutil.copy2(str(_ACCEPTED_VERSION_FILE), str(skill_root / "accepted-version.json"))
    wrapper = scripts_dir / "grok_agent.py"

    pkg = repo / "pkg"
    pkg.mkdir()
    manifest = {"name": "grok-code-probe", "scripts": {"build": "echo built"}}
    (pkg / "package.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    (pkg / "README.md").write_text("Disposable probe workspace for the slugify task.\n", encoding="utf-8")
    # A root pnpm lockfile so ProjectConfig detects pnpm as the build-gate package
    # manager (the pnpm stand-in is injected via GROK_PACKAGE_MANAGER_BINARY).
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    _git_plain(repo, "add", "-A")
    _git_plain(repo, "commit", "-q", "-m", "disposable probe repo")

    pnpm_stub = root / "pnpm_stub.sh"
    pnpm_stub.write_text(_PNPM_STUB_SCRIPT, encoding="utf-8")
    os.chmod(str(pnpm_stub), 0o755)
    return repo, wrapper, pnpm_stub


def _teardown_probe_worktree(repo: pathlib.Path, worktree_path: pathlib.Path, branch: Optional[str]) -> None:
    """Force-remove a retained (dirty) probe worktree, its branch, and the sibling ownership marker.

    The worktree still carries the code run's uncommitted edits, so the wrapper's
    own `cleanup` refuses it (dirty); `git worktree remove --force` is the
    sanctioned teardown for a disposable-repo probe. Every step is best effort
    and logged so a residual never wedges the suite.
    """
    for step_args in (
        ["worktree", "remove", "--force", str(worktree_path)],
        ["worktree", "prune"],
    ):
        result = subprocess.run(
            ["git", "-C", str(repo)] + step_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            sys.stderr.write("[live_probes] worktree teardown step {} exited {}\n".format(step_args, result.returncode))
    if branch:
        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", branch],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            sys.stderr.write("[live_probes] probe branch delete exited {}\n".format(result.returncode))
    marker = pathlib.Path(str(worktree_path) + ".owner.json")
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        sys.stderr.write("[live_probes] could not remove probe worktree marker {}: {}\n".format(marker, exc))


def _assert_code_envelope(
    envelope: Dict[str, object], exit_code: int, repo: pathlib.Path, state_home: pathlib.Path, status_before: str, status_after: str
) -> Tuple[str, Optional[str]]:
    """Assert the live code envelope (spec 14.3) and return (worktreePath, worktreeBranch)."""
    _require(exit_code == 0, "code expected exit 0, got {}: {}".format(exit_code, envelope.get("error")))
    _require(envelope.get("status") == "success", "code status not success")
    worktree_path = envelope.get("worktreePath")
    _require(isinstance(worktree_path, str) and worktree_path, "worktreePath missing")
    wt = pathlib.Path(worktree_path)
    _require(not _path_is_within(wt, repo), "worktree is INSIDE the original checkout (must be external)")
    _require(_path_is_within(wt, state_home), "worktree is not under the isolated state root")
    _require(
        envelope.get("effectiveWorkingDirectory") == worktree_path,
        "effectiveWorkingDirectory is not the worktree",
    )
    _require(status_before == status_after, "the ORIGINAL temp-repo checkout changed across the code run")
    run_id = envelope.get("runId")
    _require(isinstance(run_id, str) and run_id, "runId missing")
    sentinel = ".grok-run-" + run_id
    _require((wt / sentinel).exists(), "cwd sentinel absent from the worktree")
    _require(not (repo / sentinel).exists(), "cwd sentinel leaked into the original checkout")
    commands = envelope.get("commands")
    _require(isinstance(commands, list), "commands[] missing")
    gate = [c for c in commands if isinstance(c, dict) and str(c.get("purpose", "")).startswith("build-gate")]
    _require(bool(gate), "no build-gate command was recorded")
    _require(all(c.get("exitStatus") == 0 for c in gate), "a build-gate command did not exit 0")
    cleanup = envelope.get("cleanup") if isinstance(envelope.get("cleanup"), dict) else {}
    _require(cleanup.get("status") == "retained", "worktree not retained: {}".format(cleanup))
    _require(cleanup.get("detail") == worktree_path, "cleanup detail is not the worktree path")
    _require(wt.is_dir(), "retained worktree directory does not exist")
    sandbox = envelope.get("sandbox") if isinstance(envelope.get("sandbox"), dict) else {}
    _require(sandbox.get("enforced") is True, "sandbox write-confinement not enforced (enforced!=true)")
    _require(sandbox.get("reportedProfile") == "workspace", "sandbox profile is not workspace")
    phases = _read_progress_phases(envelope)
    _require("cleanup" in phases, "progress stream missing the private-home cleanup phase")
    warnings = envelope.get("warnings") if isinstance(envelope.get("warnings"), list) else []
    _require(
        not any("home teardown" in str(w).lower() for w in warnings),
        "private home teardown reported a warning: {}".format(warnings),
    )
    changed = envelope.get("changedFiles") if isinstance(envelope.get("changedFiles"), list) else []
    _require(
        any("slugify" in str(f) for f in changed),
        "code run did not author slugify.py (changedFiles={})".format(changed),
    )
    return worktree_path, envelope.get("worktreeBranch") if isinstance(envelope.get("worktreeBranch"), str) else None


def probe_code_and_verify(tmp_root: pathlib.Path) -> List[ProbeResult]:
    """Probes 7+8 (gating): live write-capable `code` in a disposable repo, then `verify` over the SAME worktree.

    This is the write-capable authority path and the real code->verify handoff:
    verify adopts the worktree the code run left with its UNCOMMITTED edits and
    must NOT misattribute them to itself. All wrapper state is isolated under a
    throwaway XDG_STATE_HOME; the retained (dirty) worktree is force-removed in a
    finally.
    """
    code_command = "grok_agent.py code --target pkg --base HEAD (disposable repo)"
    verify_command = "grok_agent.py verify --worktree <code worktree> (handoff)"
    state_home = tmp_root / "state"
    state_home.mkdir()
    repo, wrapper, pnpm_stub = _scaffold_disposable_repo(tmp_root)
    # The wrapper tooling lives inside the disposable repo, so running it would
    # otherwise write __pycache__ bytecode into the tracked checkout and trip the
    # "original checkout untouched" assertion. PYTHONDONTWRITEBYTECODE keeps the
    # checkout byte-clean; it does not affect the wrapper's own tracked-file
    # escape scan, which only inspects tracked modifications.
    env_extra = {
        "XDG_STATE_HOME": str(state_home),
        "GROK_PACKAGE_MANAGER_BINARY": str(pnpm_stub),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    code_result: Optional[ProbeResult] = None
    verify_result: Optional[ProbeResult] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None

    try:
        code_highlights: Dict[str, object] = {}
        code_env: Dict[str, object] = {}
        try:
            status_before = _repo_status(repo)
            exit_code, code_env, _stderr, duration = _run_wrapper_at(
                wrapper,
                ["code", "--target", "pkg", "--base", "HEAD", "--task", _CODE_PROBE_TASK, "--max-turns", "40"],
                env_extra,
                _CODE_PROBE_TIMEOUT_SECONDS,
            )
            status_after = _repo_status(repo)
            code_highlights = _envelope_highlights(code_env, duration)
            worktree_path, branch = _assert_code_envelope(
                code_env, exit_code, repo, state_home, status_before, status_after
            )
            code_highlights["worktreeExternal"] = True
            code_highlights["originalCheckoutUntouched"] = True
            code_highlights["changedFiles"] = code_env.get("changedFiles")
            code_highlights["buildGateExitStatuses"] = [
                c.get("exitStatus")
                for c in (code_env.get("commands") or [])
                if isinstance(c, dict) and str(c.get("purpose", "")).startswith("build-gate")
            ]
            code_result = ProbeResult(
                "code-disposable",
                True,
                True,
                code_command,
                code_highlights,
                "code green; worktree external+retained; original checkout untouched; sandbox write-confinement enforced",
            )
        except ProbeError as exc:
            code_highlights["error"] = code_env.get("error") if isinstance(code_env, dict) else None
            worktree_path = code_env.get("worktreePath") if isinstance(code_env.get("worktreePath"), str) else worktree_path
            branch = code_env.get("worktreeBranch") if isinstance(code_env.get("worktreeBranch"), str) else branch
            code_result = ProbeResult("code-disposable", True, False, code_command, code_highlights, str(exc))

        if worktree_path is not None:
            verify_highlights: Dict[str, object] = {}
            verify_env: Dict[str, object] = {}
            try:
                exit_code, verify_env, _stderr, duration = _run_wrapper_at(
                    wrapper,
                    ["verify", "--worktree", worktree_path, "--task", _VERIFY_PROBE_TASK, "--max-turns", "30"],
                    env_extra,
                    _CODE_PROBE_TIMEOUT_SECONDS,
                )
                verify_highlights = _envelope_highlights(verify_env, duration)
                _require(
                    exit_code == 0,
                    "verify expected exit 0 (handoff must NOT misattribute the code run's edits), got {}: {}".format(
                        exit_code, verify_env.get("error")
                    ),
                )
                _require(verify_env.get("status") == "success", "verify status not success")
                verifier = verify_env.get("verifier") if isinstance(verify_env.get("verifier"), dict) else {}
                verdict = verifier.get("verdict")
                _require(verdict in ("pass", "fail", "inconclusive"), "verify verdict outside enum: {}".format(verdict))
                effective = verify_env.get("effectiveModel")
                _require(
                    verifier.get("identity") == "grok-{}".format(effective),
                    "verifier identity is not grok-<model>: {}".format(verifier.get("identity")),
                )
                changed = verify_env.get("changedFiles") if isinstance(verify_env.get("changedFiles"), list) else []
                source_edits = [str(f) for f in changed if not _is_artifact_path(str(f))]
                _require(
                    not source_edits,
                    "verify was attributed SOURCE edits (handoff regression): {}".format(source_edits),
                )
                verify_highlights["verdict"] = verdict
                verify_highlights["changedFiles"] = changed
                verify_highlights["priorCodeEditsMisattributed"] = False
                verify_result = ProbeResult(
                    "verify-handoff",
                    True,
                    True,
                    verify_command,
                    verify_highlights,
                    "verify green; verdict={}; prior code edits NOT misattributed; identity {}".format(
                        verdict, verifier.get("identity")
                    ),
                )
            except ProbeError as exc:
                verify_highlights["error"] = verify_env.get("error") if isinstance(verify_env, dict) else None
                verify_result = ProbeResult("verify-handoff", True, False, verify_command, verify_highlights, str(exc))
        else:
            verify_result = ProbeResult(
                "verify-handoff", True, False, verify_command, {}, "skipped: the code probe produced no worktree"
            )
    finally:
        if worktree_path is not None:
            _teardown_probe_worktree(repo, pathlib.Path(worktree_path), branch)

    results: List[ProbeResult] = []
    if code_result is not None:
        results.append(code_result)
    if verify_result is not None:
        results.append(verify_result)
    return results
