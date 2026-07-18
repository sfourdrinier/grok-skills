// plugin/scripts/lib/integrate.mjs
//
// Apply-on-verified-ready for integration=auto (Task 7.3).
// Apply-time revalidation (review guard 5): re-run handoff, locate patch,
// git apply --check, then apply. Never half-apply; reverse on mid-apply failure.

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { runsDirFor, safeRunIdForRunsDir } from "../progress-relay.mjs";

/**
 * Resolve runs/<runId>/artifacts/implementation.patch under the wrapper state
 * root (same XDG layout handoff uses). Null when missing or runId unsafe.
 *
 * @param {string} runId
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null}
 */
export function locateImplementationPatch(runId, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe) return null;
  const patchPath = path.join(runsDir, safe, "artifacts", "implementation.patch");
  try {
    const st = fs.statSync(patchPath);
    if (!st.isFile() || st.size <= 0) return null;
    return patchPath;
  } catch {
    return null;
  }
}

/**
 * @param {string} filePath
 * @returns {string}
 */
export function sha256File(filePath) {
  const h = createHash("sha256");
  h.update(fs.readFileSync(filePath));
  return h.digest("hex");
}

/**
 * Resolve `<runsDir>/<runId>/implementation-handoff.json` (the validation manifest
 * at the run root, one level up from `artifacts/implementation.patch`).
 * @param {string} runId
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null}
 */
export function locateHandoffManifest(runId, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe) return null;
  return path.join(runsDir, safe, "implementation-handoff.json");
}

/**
 * Verify an on-disk patch matches the validation manifest's `patch.sha256`
 * (lowercase hex) + `patch.bytes`. Fail closed on a missing/corrupt manifest or
 * any mismatch, so a patch substituted/corrupted between wrapper validation and
 * companion apply cannot land (the wrapper's handoff re-check does the equivalent
 * for the code auto path, which re-runs handoff instead of reusing this).
 * @returns {{ok: boolean, reason?: string}}
 */
export function verifyPatchAgainstManifest(runId, patchPath, env = process.env) {
  const manifestPath = locateHandoffManifest(runId, env);
  if (!manifestPath) return { ok: false, reason: "unsafe runId" };
  let doc;
  try {
    doc = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch {
    return { ok: false, reason: "manifest missing or unreadable" };
  }
  const expectedSha = doc?.patch?.sha256;
  const expectedBytes = doc?.patch?.bytes;
  if (typeof expectedSha !== "string" || !/^[0-9a-fA-F]{64}$/.test(expectedSha)) {
    return { ok: false, reason: "manifest patch.sha256 invalid" };
  }
  if (!Number.isInteger(expectedBytes) || expectedBytes < 1) {
    return { ok: false, reason: "manifest patch.bytes invalid" };
  }
  let actualBytes;
  try {
    actualBytes = fs.statSync(patchPath).size;
  } catch {
    return { ok: false, reason: "patch unreadable" };
  }
  if (actualBytes !== expectedBytes) {
    return { ok: false, reason: `patch bytes ${actualBytes} != manifest ${expectedBytes}` };
  }
  if (sha256File(patchPath).toLowerCase() !== expectedSha.toLowerCase()) {
    return { ok: false, reason: "patch sha256 does not match manifest" };
  }
  return { ok: true };
}

function git(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  return {
    code: typeof result.status === "number" ? result.status : 1,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    error: result.error || null,
  };
}

/** Strip git's double-quoted path quoting (special-char paths). */
function unquoteGitPath(p) {
  const s = String(p).trim();
  if (s.startsWith('"') && s.endsWith('"')) {
    try {
      return JSON.parse(s);
    } catch {
      return s.slice(1, -1);
    }
  }
  return s;
}

/**
 * Both sides of a numstat rename form ("old => new", "{a => b}/f") - a dirty
 * `old` path being renamed carries the operator's edits into `new`, so the
 * dirty-overlap guard must consider both (review). Non-renames return [p].
 */
function renamePathSides(p) {
  const s = String(p);
  const collapse = (x) => x.replace(/\/{2,}/g, "/");
  const brace = s.match(/^(.*)\{([^}]*?) => ([^}]*?)\}(.*)$/);
  if (brace) {
    const [, pre, oldMid, newMid, post] = brace;
    return [collapse(pre + oldMid + post), collapse(pre + newMid + post)];
  }
  const idx = s.indexOf(" => ");
  if (idx >= 0) return [collapse(s.slice(0, idx)), collapse(s.slice(idx + 4))];
  return [collapse(s)];
}

/**
 * Repo-relative dirty paths from `git status --porcelain -z --untracked-files=all`.
 * @param {string} statusOutput
 * @returns {Set<string>}
 */
export function parseDirtyStatusPaths(statusOutput) {
  // Input is `git status --porcelain -z --untracked-files=all`: NUL-TERMINATED
  // entries with paths NOT quoted (no `"..."`, no ` -> ` arrow). A rename/copy
  // entry (R/C in either status column) is followed by a SECOND NUL-token holding
  // the paired path. We add BOTH the status-line path and any paired path
  // (direction-agnostic), so the overlap guard catches either name - and a literal
  // ` -> ` (even a quoted one) inside a filename can never be mis-split.
  const set = new Set();
  const entries = String(statusOutput || "").split("\0");
  for (let i = 0; i < entries.length; i++) {
    const raw = entries[i];
    if (!raw) continue; // trailing empty token after the final NUL
    const xy = raw.slice(0, 2);
    const p = raw.slice(3); // "XY " prefix: 2 status columns + 1 space
    if (p) set.add(p);
    if (xy[0] === "R" || xy[0] === "C" || xy[1] === "R" || xy[1] === "C") {
      i += 1;
      const paired = entries[i]; // the rename/copy source path (raw, no prefix)
      if (paired) set.add(paired);
    }
  }
  return set;
}

/**
 * Repo-relative paths a patch touches, from `git apply --numstat --binary`.
 * @param {string} numstatOutput
 * @returns {string[]}
 */
export function parseNumstatPaths(numstatOutput) {
  const paths = [];
  for (const raw of String(numstatOutput || "").split("\n")) {
    if (!raw.trim()) continue;
    const parts = raw.split("\t"); // "<added>\t<deleted>\t<path>"
    if (parts.length < 3) continue;
    const pathField = parts.slice(2).join("\t");
    const sides = renamePathSides(pathField);
    for (const side of sides) paths.push(unquoteGitPath(side));
    // If the field LOOKED like a rename (split changed it), also keep the raw
    // field: a real filename literally containing " => " / "{...}" (git does not
    // quote those) would be mis-split, so the raw path keeps the dirty-overlap
    // guard from failing open. No duplicate for ordinary paths.
    if (sides.length !== 1 || sides[0] !== pathField) {
      paths.push(unquoteGitPath(pathField));
    }
  }
  return paths;
}

/**
 * Apply-time revalidation + git apply to the operator target tree.
 * Caller supplies runHandoff (typically runHandoffCaptured) to avoid import cycles.
 *
 * @param {object} opts
 * @param {string} opts.wrapper
 * @param {string} opts.runId
 * @param {string} opts.targetRepo absolute target workspace root
 * @param {(wrapper: string, args: string[], opts?: object) => { code: number, envelope: object|null }} opts.runHandoff
 * @param {(line: string) => void} [opts.stderrLine]
 * @param {NodeJS.ProcessEnv} [opts.env]
 * @returns {{
 *   ok: boolean,
 *   outcome: string,
 *   reason?: string,
 *   runId: string,
 *   patchPath?: string,
 *   patchSha?: string,
 *   preStatus?: string,
 *   postStatus?: string,
 * }}
 */
export function applyVerifiedPatch({
  wrapper,
  runId,
  targetRepo,
  runHandoff,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
  env = process.env,
} = {}) {
  if (typeof runHandoff !== "function") {
    return {
      ok: false,
      outcome: "blocked-internal",
      reason: "runHandoff callback required",
      runId: runId || "",
    };
  }
  if (!runId || !targetRepo) {
    return {
      ok: false,
      outcome: "blocked-internal",
      reason: "runId and targetRepo required",
      runId: runId || "",
    };
  }

  // 1. Fresh dual-condition re-read (do not trust the earlier handoff read).
  stderrLine(`[grok-auto] apply-time revalidation for ${runId}`);
  const { code: hCode, envelope: hEnv } = runHandoff(wrapper, [
    "handoff",
    "--run-id",
    runId,
  ]);
  const ready = hEnv?.response?.integration?.ready === true;
  if (hCode !== 0 || !ready) {
    stderrLine(
      `[grok-auto] BLOCKED: apply-time handoff revalidation not ready for ${runId} ` +
        `(handoff exit ${hCode}, ready=${ready})`
    );
    return {
      ok: false,
      outcome: "blocked-revalidation",
      reason: "apply-time handoff revalidation not ready",
      runId,
    };
  }

  // 2. Locate patch under wrapper state root.
  const patchPath = locateImplementationPatch(runId, env);
  if (!patchPath) {
    stderrLine(
      `[grok-auto] BLOCKED: cannot locate implementation.patch for ${runId} ` +
        `under ${runsDirFor(env)}`
    );
    return {
      ok: false,
      outcome: "blocked-patch-missing",
      reason: "implementation.patch not found under state root",
      runId,
    };
  }

  const patchSha = sha256File(patchPath);
  const preStatus = git(targetRepo, ["status", "--porcelain", "-z", "--untracked-files=all"]);

  // 3a. Dirty-overlap guard: git apply --check can PASS even when the patch
  // touches a file the operator is actively editing (non-conflicting hunks).
  // Auto-apply to the live tree must NOT entangle Grok's changes into a dirty
  // file - block before apply (operator commits/stashes, then re-runs).
  const dirtyPaths = parseDirtyStatusPaths(preStatus.stdout);
  const numstat = git(targetRepo, ["apply", "--numstat", "--binary", patchPath]);
  const patchPaths = numstat.code === 0 ? parseNumstatPaths(numstat.stdout) : [];
  const overlap = patchPaths.filter((p) => dirtyPaths.has(p)).sort();
  if (overlap.length > 0) {
    stderrLine(
      `[grok-auto] BLOCKED: patch overlaps already-dirty path(s): ${overlap.join(", ")}. ` +
        `Commit or stash them, then re-run. No apply attempted.`
    );
    return {
      ok: false,
      outcome: "blocked-dirty-overlap",
      reason: "patch touches paths already dirty in the operator checkout",
      runId,
      patchPath,
      patchSha,
      overlap,
      preStatus: preStatus.stdout,
    };
  }

  // 3. Precondition: git apply --check --binary (tree may have moved since run).
  const check = git(targetRepo, ["apply", "--check", "--binary", patchPath]);
  if (check.code !== 0) {
    stderrLine(
      `[grok-auto] BLOCKED: git apply --check failed (target tree moved since run). ` +
        `PARTIAL/blocked - no apply attempted. pre-status:\n${preStatus.stdout || "(clean)"}`
    );
    if (check.stderr) stderrLine(check.stderr.trimEnd());
    return {
      ok: false,
      outcome: "blocked-apply-check",
      reason: "git apply --check failed; target tree incompatible with patch",
      runId,
      patchPath,
      patchSha,
      preStatus: preStatus.stdout,
      checkStderr: check.stderr,
    };
  }

  // 4. Apply. On failure mid-apply, attempt reverse to restore.
  const apply = git(targetRepo, ["apply", "--binary", patchPath]);
  if (apply.code !== 0) {
    stderrLine(`[grok-auto] apply failed; attempting reverse (git apply -R) to restore`);
    if (apply.stderr) stderrLine(apply.stderr.trimEnd());
    const rev = git(targetRepo, ["apply", "-R", "--binary", patchPath]);
    if (rev.code === 0) {
      stderrLine(`[grok-auto] rolled-back via git apply -R; target restored`);
      return {
        ok: false,
        outcome: "rolled-back",
        reason: "git apply failed; reverse succeeded",
        runId,
        patchPath,
        patchSha,
      };
    }
    stderrLine(
      `[grok-auto] reverse also failed; MANUAL-NEEDED - inspect target tree for partial apply`
    );
    return {
      ok: false,
      outcome: "manual-needed",
      reason: "git apply failed and reverse failed",
      runId,
      patchPath,
      patchSha,
    };
  }

  const postStatus = git(targetRepo, ["status", "--short", "--untracked-files=all"]);
  stderrLine(
    `[grok-auto] APPLIED runId=${runId} patchSha=${patchSha}\n` +
      `pre-status:\n${preStatus.stdout || "(clean)"}\n` +
      `post-status:\n${postStatus.stdout || "(clean)"}`
  );
  return {
    ok: true,
    outcome: "applied",
    runId,
    patchPath,
    patchSha,
    preStatus: preStatus.stdout,
    postStatus: postStatus.stdout,
  };
}

// --- Peer-stop integration (Task 7.4, extracted from grok-companion.mjs to
// keep the companion under the 900-line cap; reuses applyVerifiedPatch). ---
import { tryParseEnvelope } from "./render.mjs";
import { sanitizeRunId } from "./companion-terminal-notify.mjs";
import {
  parseIntegrationMode,
  getIntegrationMode,
  getIntegrationConsent,
  formatDirectIntegrationConsentMsg,
} from "./jobs.mjs";
import { resolveTargetWorkspaceRoot, parseTargetFlag } from "./git-context.mjs";

/**
 * Exit code for a peer-stop: a requested peer-stop apply that FAILED (moved tree
 * / dirty overlap / half-apply) fails the command instead of the wrapper's 0.
 */
export function peerStopExitCode(wrapperCode, peerIntegration) {
  if (peerIntegration && peerIntegration.attempted && !peerIntegration.ok) {
    return typeof wrapperCode === "number" && wrapperCode !== 0 ? wrapperCode : 1;
  }
  return wrapperCode;
}

/** ACP default; GROK_DISABLE_ACP=1 opt-out (Task 7.4). */
export function isAcpDisabled(env = process.env) {
  const f = String(env.GROK_DISABLE_ACP ?? "").trim().toLowerCase();
  return f === "1" || f === "true" || f === "yes" || f === "on";
}

/** True when rest carries an explicit --target (split or equals form). */
function hasTargetFlag(rest) {
  return (
    Array.isArray(rest) &&
    rest.some((a) => a === "--target" || (typeof a === "string" && a.startsWith("--target=")))
  );
}

/**
 * On a READY peer-stop: auto/direct apply the verified patch to the target
 * tree; review/worktree retain it. Returns an outcome so the caller can fail the
 * command when a requested apply did not happen.
 * @param {(line: string) => void} stderrLine
 * @returns {{attempted: boolean, ok: boolean, outcome: string}}
 */
export function maybeIntegratePeerStop(stdout, cwd, integrationFlag, rest, stderrLine) {
  const env = tryParseEnvelope(stdout || "");
  const ready =
    env?.response?.peer?.integrationReady === true ||
    env?.response?.integration?.ready === true;
  if (!ready || env?.status !== "success") return { attempted: false, ok: true, outcome: "not-ready" };
  const repo = env?.repository;
  // SECURITY: the peer patch belongs to env.repository and is ALWAYS applied there
  // (git(repo, ...) below). Consent/mode MUST be gated on THAT repo, never on a
  // supplied --target - otherwise a --target naming repo A (which the operator has
  // consented) could authorize applying the patch to repo B from the envelope
  // (consent laundering / cross-repo apply). The documented peer-stop form has no
  // --target; if one is given it must resolve to the SAME repo, else refuse.
  const tWs = resolveTargetWorkspaceRoot(cwd, repo || env?.targetWorkspace || ".");
  if (hasTargetFlag(rest)) {
    const targetWs = resolveTargetWorkspaceRoot(cwd, parseTargetFlag(rest));
    if (path.resolve(targetWs) !== path.resolve(tWs)) {
      stderrLine(
        `[grok-peer] --target ${JSON.stringify(parseTargetFlag(rest))} does not resolve to the ` +
          `peer repository ${JSON.stringify(repo)}; refusing (peer-stop applies to the peer's own repo)`
      );
      return { attempted: true, ok: false, outcome: "target-mismatch" };
    }
  }
  const mode =
    integrationFlag != null && String(integrationFlag).trim() !== ""
      ? parseIntegrationMode(integrationFlag)
      : getIntegrationMode(tWs);
  if (!mode) {
    stderrLine(
      `[grok-companion] invalid --integration ${JSON.stringify(integrationFlag)} ` +
        `(valid: direct|worktree|auto|review)`
    );
    return { attempted: true, ok: false, outcome: "invalid-mode" };
  }
  if (mode === "worktree" || mode === "review") {
    stderrLine(`[grok-peer] integration=${mode}: patch retained; not applied`);
    return { attempted: false, ok: true, outcome: "retained" };
  }
  if (mode === "direct" && !getIntegrationConsent(tWs)) {
    stderrLine(formatDirectIntegrationConsentMsg({ targetWorkspace: tWs, companionCwd: cwd }));
    // Fail closed: direct was the REQUESTED integration but consent blocked the
    // apply. Unlike worktree/review (retained by design), returning ok:true here
    // would let peerStopExitCode preserve the wrapper's 0 exit, so the command
    // would look successful while the verified patch was never applied. Mark it
    // an attempted-but-failed integration so callers see a nonzero exit, parity
    // with the code/direct consent gate.
    return { attempted: true, ok: false, outcome: "consent-required" };
  }
  const runId = sanitizeRunId(env?.runId);
  if (!runId || typeof repo !== "string" || !repo) {
    stderrLine("[grok-peer] missing runId or repository on peer-stop envelope");
    return { attempted: true, ok: false, outcome: "missing-run-or-repo" };
  }
  const patchPath = locateImplementationPatch(runId);
  if (!patchPath) {
    stderrLine(`[grok-peer] patch missing for run ${runId}`);
    return { attempted: true, ok: false, outcome: "patch-missing" };
  }
  // Re-verify the patch bytes/sha against the peer-stop validation manifest before
  // touching it (peer-stop skips the handoff re-run, so without this a patch
  // substituted or corrupted between wrapper validation and companion apply could
  // land while the command reports a validated peer result). A ready peer-stop
  // always wrote a manifest, so missing/corrupt/mismatch = tampering -> fail closed.
  const integrity = verifyPatchAgainstManifest(runId, patchPath);
  if (!integrity.ok) {
    stderrLine(
      `[grok-peer] BLOCKED: patch integrity check failed for ${runId}: ${integrity.reason}`
    );
    return { attempted: true, ok: false, outcome: "patch-integrity-failure" };
  }
  // Reuse the module git() helper (64 MB maxBuffer): the default 1 MB buffer
  // would truncate a large `git status`/`--numstat`, making the dirty-overlap
  // guard below see empty input and FAIL OPEN (apply anyway). Same helper the
  // auto path uses.
  const g = (a) => git(repo, a);
  // Dirty-overlap guard (same as the auto path): git apply --check can pass when
  // the patch touches an already-dirty file with non-conflicting hunks, silently
  // entangling Grok's changes with the operator's edits.
  const preStatus = g(["status", "--porcelain", "-z", "--untracked-files=all"]);
  const dirtyPaths = parseDirtyStatusPaths(preStatus.stdout || "");
  const numstat = g(["apply", "--numstat", "--binary", patchPath]);
  const patchPaths = numstat.code === 0 ? parseNumstatPaths(numstat.stdout || "") : [];
  const overlap = patchPaths.filter((p) => dirtyPaths.has(p)).sort();
  if (overlap.length > 0) {
    stderrLine(
      `[grok-peer] BLOCKED: patch overlaps already-dirty path(s): ${overlap.join(", ")}. ` +
        `Commit or stash them, then re-run. No apply attempted.`
    );
    return { attempted: true, ok: false, outcome: "blocked-dirty-overlap" };
  }
  // Peer-stop already ran real validation, so we do not re-run handoff; still
  // guard the apply with git apply --check (TOCTOU: the tree may have moved).
  const check = g(["apply", "--check", "--binary", patchPath]);
  if (check.code !== 0) {
    stderrLine(`[grok-peer] git apply --check failed: ${(check.stderr || check.stdout || "").trim()}`);
    return { attempted: true, ok: false, outcome: "blocked-apply-check" };
  }
  const apply = g(["apply", "--binary", patchPath]);
  if (apply.code !== 0) {
    const detail = (apply.stderr || apply.stdout || "").trim();
    // Never leave a half-applied tree: reverse (git apply -R) like the auto path.
    const rev = g(["apply", "-R", "--binary", patchPath]);
    if (rev.code === 0) {
      stderrLine(`[grok-peer] git apply failed; rolled back via -R: ${detail}`);
      return { attempted: true, ok: false, outcome: "rolled-back" };
    }
    stderrLine(
      `[grok-peer] git apply failed AND reverse failed; MANUAL-NEEDED ` +
        `(inspect ${repo} for partial apply): ${detail}`
    );
    return { attempted: true, ok: false, outcome: "manual-needed" };
  }
  stderrLine(`[grok-peer] applied ${patchPath} to ${repo}`);
  return { attempted: true, ok: true, outcome: "applied" };
}
