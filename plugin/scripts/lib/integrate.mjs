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
import { tryParseEnvelope } from "./render.mjs";
import { sanitizeRunId } from "./companion-terminal-notify.mjs";
import {
  parseIntegrationMode,
  getIntegrationMode,
  getIntegrationConsent,
  formatDirectIntegrationConsentMsg,
} from "./jobs.mjs";
import { resolveTargetWorkspaceRoot, parseTargetFlag } from "./git-context.mjs";
import {
  unquoteGitPath,
  parseDirtyStatusPaths,
  parseNumstatPaths,
  parseDiffGitHeaderPaths,
  pathsFromGitPatch,
} from "./integrate-paths.mjs";
import {
  targetIdentityKey,
  locateApplyMarker,
  readMatchingApplyMarker,
  writeApplyMarker,
  clearApplyMarker,
  acquireApplyLock,
} from "./integrate-apply-state.mjs";

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
 * any mismatch, so a patch substituted/corrupted between wrapper validation
 * (or apply-time handoff revalidation) and companion apply cannot land.
 * Shared by peer-stop and code auto apply paths.
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

// Re-export path/lock SSOTs for tests and callers that import from integrate.mjs.
export {
  unquoteGitPath,
  parseDirtyStatusPaths,
  parseNumstatPaths,
  parseDiffGitHeaderPaths,
  pathsFromGitPatch,
} from "./integrate-paths.mjs";
export {
  targetIdentityKey,
  locateApplyMarker,
  readMatchingApplyMarker,
  writeApplyMarker,
  clearApplyMarker,
  acquireApplyLock,
} from "./integrate-apply-state.mjs";

/**
 * True when the working tree still contains the applied patch (reverse --check
 * succeeds). Used so a durable marker cannot claim already-applied after the
 * operator reverts the patch, and so a crash after apply (no marker yet) can
 * heal under lock before the dirty-overlap guard.
 * @param {string} targetRepo
 * @param {string} patchPath
 * @returns {boolean}
 */
function treeStillHasAppliedPatch(targetRepo, patchPath) {
  const revCheck = git(targetRepo, ["apply", "-R", "--check", "--binary", patchPath]);
  return revCheck.code === 0 && !revCheck.error;
}

/**
 * Union numstat destinations with diff --git / rename-copy headers.
 * Non-empty numstat makes headers load-bearing: empty/unparseable headers and
 * uncorroborated simple numstat paths fail closed (no numstat-only fallback).
 * @returns {{ok: true, paths: string[]} | {ok: false, outcome: string, reason: string}}
 */
export function loadPatchTouchPaths(patchPath, numstatStdout) {
  const numstatPaths = parseNumstatPaths(numstatStdout);
  const patchPathSet = new Set(numstatPaths);
  let patchBytes;
  try {
    patchBytes = fs.readFileSync(patchPath);
  } catch {
    return {
      ok: false,
      outcome: "blocked-patch-headers",
      reason: "patch header read failed after numstat; cannot compute full dirty touch set",
    };
  }
  let headerPaths;
  try {
    headerPaths = pathsFromGitPatch(patchBytes);
  } catch {
    return {
      ok: false,
      outcome: "blocked-patch-headers",
      reason: "patch header parse failed after numstat; cannot compute full dirty touch set",
    };
  }
  if (numstatPaths.length > 0) {
    if (!headerPaths || headerPaths.size === 0) {
      return {
        ok: false,
        outcome: "blocked-patch-headers",
        reason:
          "patch headers empty/unparseable after non-empty numstat; cannot compute full dirty touch set",
      };
    }
    // Skip synthetic raw "a => b" fields kept by parseNumstatPaths for resilience.
    for (const p of numstatPaths) {
      if (!p || p.includes(" => ") || p.includes("{")) continue;
      if (!headerPaths.has(p)) {
        return {
          ok: false,
          outcome: "blocked-patch-headers",
          reason:
            "numstat path not corroborated by patch headers (rename/copy destination or touch gap)",
        };
      }
    }
  }
  for (const p of headerPaths) patchPathSet.add(p);
  return { ok: true, paths: [...patchPathSet] };
}

/**
 * After a successful apply, persist the durable marker. If persistence fails,
 * reverse the apply so we never report durable applied success without a marker.
 * @returns {{ok: boolean, outcome: string, reason?: string, patchPath?: string}}
 */
function finalizeAppliedWithMarker({
  targetRepo,
  patchPath,
  runId,
  targetKey,
  patchSha,
  env,
  stderrLine,
  logTag,
  spine,
}) {
  const tag = `[${logTag}]`;
  const wrote = writeApplyMarker(runId, targetKey, patchSha, env);
  if (wrote) {
    return { ...spine, runId, patchPath: spine.patchPath || patchPath, patchSha };
  }
  stderrLine(
    `${tag} BLOCKED: applied patch but durable marker write failed; reversing apply`
  );
  const rev = git(targetRepo, ["apply", "-R", "--binary", patchPath]);
  if (rev.code === 0) {
    clearApplyMarker(runId, targetKey, env);
    return {
      ok: false,
      outcome: "marker-persist-failure",
      reason: "applied but durable marker write failed; reversed",
      runId,
      patchPath,
      patchSha,
    };
  }
  return {
    ok: false,
    outcome: "manual-needed",
    reason: "applied but durable marker write failed and reverse failed",
    runId,
    patchPath,
    patchSha,
  };
}

/**
 * Shared dirty-guard + apply spine used by both auto and peer.
 * Status fail-closed, numstat fail-closed, header-union fail-closed, dirty-overlap,
 * apply --check, apply, reverse rollback. Callers keep readiness / consent /
 * target identity / patch-integrity gates outside this helper.
 *
 * Published outcomes: blocked-dirty-status, blocked-numstat, blocked-patch-headers,
 * blocked-dirty-overlap, blocked-apply-check, applied, rolled-back, manual-needed.
 *
 * @param {object} opts
 * @param {string} opts.targetRepo absolute target workspace root
 * @param {string} opts.patchPath absolute path to the patch file
 * @param {(line: string) => void} opts.stderrLine
 * @param {string} [opts.logTag="grok"] prefix tag without brackets (e.g. "grok-auto")
 * @param {(ctx: {preStatus: string, postStatus: string}) => void} [opts.onApplied]
 * @returns {{
 *   ok: boolean,
 *   outcome: string,
 *   reason?: string,
 *   patchPath?: string,
 *   overlap?: string[],
 *   preStatus?: string,
 *   postStatus?: string,
 *   checkStderr?: string,
 * }}
 */
function applyPatchWithGuards({
  targetRepo,
  patchPath,
  stderrLine,
  logTag = "grok",
  onApplied,
} = {}) {
  const tag = `[${logTag}]`;
  // Fail closed: a failed/killed `git status` (e.g. maxBuffer overflow in a
  // very dirty checkout, or a truncated stdout) yields an incomplete dirty set,
  // and `git apply --check` alone can pass on a non-conflicting hunk in a dirty
  // file. Without a trustworthy dirty list we cannot compute overlap - do not
  // apply blind. (64 MB maxBuffer via git() - same helper auto and peer use.)
  const preStatus = git(targetRepo, ["status", "--porcelain", "-z", "--untracked-files=all"]);
  if (preStatus.code !== 0 || preStatus.error) {
    stderrLine(
      `${tag} BLOCKED: git status failed (code=${preStatus.code}); cannot compute dirty overlap`
    );
    return {
      ok: false,
      outcome: "blocked-dirty-status",
      reason: "git status failed; cannot compute dirty overlap",
    };
  }

  // Dirty-overlap guard: git apply --check can PASS even when the patch
  // touches a file the operator is actively editing (non-conflicting hunks).
  // Auto/peer apply to the live tree must NOT entangle Grok's changes into a
  // dirty file - block before apply (operator commits/stashes, then re-runs).
  const dirtyPaths = parseDirtyStatusPaths(preStatus.stdout);
  const numstat = git(targetRepo, ["apply", "--numstat", "--binary", patchPath]);
  if (numstat.code !== 0) {
    // Fail closed: without the patch's path list we cannot compute dirty overlap,
    // and `git apply --check` alone can pass on a non-conflicting hunk in a dirty
    // file. Do not apply blind.
    stderrLine(`${tag} BLOCKED: git apply --numstat failed; cannot verify dirty overlap`);
    return {
      ok: false,
      outcome: "blocked-numstat",
      reason: "numstat failed; cannot compute dirty overlap",
    };
  }
  // numstat is destination-biased on pure renames; union diff --git / rename-copy
  // from/to sides so a dirty SOURCE cannot fail the overlap guard open. Header
  // read/parse failure after successful numstat fails closed (never numstat-only).
  const touch = loadPatchTouchPaths(patchPath, numstat.stdout);
  if (!touch.ok) {
    stderrLine(
      `${tag} BLOCKED: ${touch.reason || "cannot read patch headers after numstat"}`
    );
    return {
      ok: false,
      outcome: touch.outcome,
      reason: touch.reason,
      patchPath,
    };
  }
  const patchPaths = touch.paths;
  const overlap = patchPaths.filter((p) => dirtyPaths.has(p)).sort();
  if (overlap.length > 0) {
    stderrLine(
      `${tag} BLOCKED: patch overlaps already-dirty path(s): ${overlap.join(", ")}. ` +
        `Commit or stash them, then re-run. No apply attempted.`
    );
    return {
      ok: false,
      outcome: "blocked-dirty-overlap",
      reason: "patch touches paths already dirty in the operator checkout",
      patchPath,
      overlap,
      preStatus: preStatus.stdout,
    };
  }

  // Precondition: git apply --check --binary (tree may have moved since run).
  const check = git(targetRepo, ["apply", "--check", "--binary", patchPath]);
  if (check.code !== 0) {
    stderrLine(
      `${tag} BLOCKED: git apply --check failed (target tree moved since run). ` +
        `PARTIAL/blocked - no apply attempted. pre-status:\n${preStatus.stdout || "(clean)"}`
    );
    if (check.stderr) stderrLine(check.stderr.trimEnd());
    return {
      ok: false,
      outcome: "blocked-apply-check",
      reason: "git apply --check failed; target tree incompatible with patch",
      patchPath,
      preStatus: preStatus.stdout,
      checkStderr: check.stderr,
    };
  }

  // Apply. On failure mid-apply, attempt reverse to restore.
  const apply = git(targetRepo, ["apply", "--binary", patchPath]);
  if (apply.code !== 0) {
    const detail = (apply.stderr || apply.stdout || "").trim();
    stderrLine(`${tag} apply failed; attempting reverse (git apply -R) to restore`);
    if (detail) stderrLine(detail);
    const rev = git(targetRepo, ["apply", "-R", "--binary", patchPath]);
    if (rev.code === 0) {
      stderrLine(`${tag} rolled-back via git apply -R; target restored`);
      return {
        ok: false,
        outcome: "rolled-back",
        reason: "git apply failed; reverse succeeded",
        patchPath,
      };
    }
    stderrLine(
      `${tag} reverse also failed; MANUAL-NEEDED - inspect target tree for partial apply` +
        (detail ? `: ${detail}` : "")
    );
    return {
      ok: false,
      outcome: "manual-needed",
      reason: "git apply failed and reverse failed",
      patchPath,
    };
  }

  const postStatus = git(targetRepo, ["status", "--short", "--untracked-files=all"]);
  if (typeof onApplied === "function") {
    onApplied({ preStatus: preStatus.stdout, postStatus: postStatus.stdout });
  } else {
    stderrLine(`${tag} applied ${patchPath} to ${targetRepo}`);
  }
  return {
    ok: true,
    outcome: "applied",
    patchPath,
    preStatus: preStatus.stdout,
    postStatus: postStatus.stdout,
  };
}

/**
 * Shared under-lock apply ladder for auto and peer (one source of truth).
 * Callers keep readiness / consent / target / pre-lock integrity outside.
 * revalidateUnderLock runs before heal marker write and before apply spine.
 *
 * Ladder: matching marker + reverse => already-applied; marker but reverted =>
 * clear; no marker + reverse => revalidate then heal marker; else revalidate,
 * applyPatchWithGuards, finalizeAppliedWithMarker.
 *
 * @param {object} opts
 * @returns {{ok: boolean, outcome: string, runId?: string, patchPath?: string, patchSha?: string, reason?: string, overlap?: string[]}}
 */
export function completeIntegrationApplyUnderLock({
  targetRepo,
  patchPath,
  runId,
  targetKey,
  patchSha,
  env = process.env,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
  logTag = "grok",
  onApplied,
  revalidateUnderLock,
} = {}) {
  const tag = `[${logTag}]`;
  const prior = readMatchingApplyMarker(runId, targetKey, patchSha, env);
  if (prior.matched) {
    if (treeStillHasAppliedPatch(targetRepo, patchPath)) {
      stderrLine(
        `${tag} already-applied runId=${runId} patchSha=${patchSha} target=${targetKey}`
      );
      return {
        ok: true,
        outcome: "already-applied",
        runId,
        patchPath,
        patchSha,
      };
    }
    // Marker exists but operator reverted the tree - clear and re-apply.
    stderrLine(
      `${tag} applied marker present but tree no longer has patch; re-applying`
    );
    clearApplyMarker(runId, targetKey, env);
  } else if (treeStillHasAppliedPatch(targetRepo, patchPath)) {
    // Crash-after-apply residue: revalidate under lock BEFORE healing marker.
    stderrLine(
      `${tag} tree already has applied patch without durable marker; healing marker`
    );
    if (typeof revalidateUnderLock === "function") {
      const v = revalidateUnderLock();
      if (v && v.ok === false) return v;
    }
    const wrote = writeApplyMarker(runId, targetKey, patchSha, env);
    if (wrote) {
      stderrLine(
        `${tag} already-applied runId=${runId} patchSha=${patchSha} target=${targetKey}`
      );
      return {
        ok: true,
        outcome: "already-applied",
        runId,
        patchPath,
        patchSha,
      };
    }
    stderrLine(
      `${tag} BLOCKED: applied tree lacks durable marker and marker heal failed`
    );
    return {
      ok: false,
      outcome: "marker-persist-failure",
      reason: "tree has applied patch but durable marker write failed",
      runId,
      patchPath,
      patchSha,
    };
  }

  if (typeof revalidateUnderLock === "function") {
    const v = revalidateUnderLock();
    if (v && v.ok === false) return v;
  }

  const spine = applyPatchWithGuards({
    targetRepo,
    patchPath,
    stderrLine,
    logTag,
    onApplied,
  });
  if (spine.ok && spine.outcome === "applied") {
    return finalizeAppliedWithMarker({
      targetRepo,
      patchPath,
      runId,
      targetKey,
      patchSha,
      env,
      stderrLine,
      logTag,
      spine,
    });
  }
  const out = { ...spine, runId };
  if (
    spine.outcome !== "blocked-dirty-status" &&
    spine.outcome !== "blocked-numstat" &&
    spine.outcome !== "blocked-patch-headers"
  ) {
    out.patchPath = spine.patchPath || patchPath;
    out.patchSha = patchSha;
  }
  return out;
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

  // locateImplementationPatch only stat-checked the file; it can still vanish or
  // become unreadable before this hash. Convert that race into a BLOCKED outcome
  // (not an exception): auto suppresses stdout until it builds the final envelope,
  // so an uncaught throw here would skip the final envelope + job finalization and
  // leave the code-leg job/output looking successful.
  let patchSha;
  try {
    patchSha = sha256File(patchPath);
  } catch (err) {
    stderrLine(`[grok-auto] BLOCKED: cannot read patch for ${runId}: ${err.message}`);
    return {
      ok: false,
      outcome: "blocked-patch-unreadable",
      reason: "patch read/hash failed",
      runId,
    };
  }

  // Re-verify current implementation.patch bytes/size/hash against the
  // revalidated handoff manifest immediately before the shared apply ladder.
  // Handoff ready does not freeze the artifact: a substitute/corruption between
  // revalidation and git apply must fail closed (same SSOT peer uses).
  const integrity = verifyPatchAgainstManifest(runId, patchPath, env);
  if (!integrity.ok) {
    stderrLine(
      `[grok-auto] BLOCKED: patch integrity check failed for ${runId}: ${integrity.reason}`
    );
    return {
      ok: false,
      outcome: "patch-integrity-failure",
      reason: integrity.reason || "patch integrity check failed",
      runId,
      patchPath,
      patchSha,
    };
  }

  // 3. Exclusive per-(runId, target) apply lock + shared under-lock ladder.
  // Policy/integrity gates stay above; lock body is completeIntegrationApplyUnderLock.
  // Never reverse another winner; rollback only mid-apply failure of this holder.
  const targetKey = targetIdentityKey(targetRepo);
  let releaseLock = null;
  try {
    releaseLock = acquireApplyLock(runId, targetKey, env);
  } catch (err) {
    stderrLine(`[grok-auto] BLOCKED: cannot acquire apply lock for ${runId}: ${err.message}`);
    return {
      ok: false,
      outcome: "blocked-apply-lock",
      reason: err.message || "apply lock failed",
      runId,
      patchPath,
      patchSha,
    };
  }
  try {
    return completeIntegrationApplyUnderLock({
      targetRepo,
      patchPath,
      runId,
      targetKey,
      patchSha,
      env,
      stderrLine,
      logTag: "grok-auto",
      onApplied: ({ preStatus, postStatus }) => {
        stderrLine(
          `[grok-auto] APPLIED runId=${runId} patchSha=${patchSha}\n` +
            `pre-status:\n${preStatus || "(clean)"}\n` +
            `post-status:\n${postStatus || "(clean)"}`
        );
      },
      revalidateUnderLock: () => {
        // Re-verify integrity under lock (artifact can change between pre-lock hash and apply).
        const lockedIntegrity = verifyPatchAgainstManifest(runId, patchPath, env);
        if (!lockedIntegrity.ok) {
          stderrLine(
            `[grok-auto] BLOCKED: patch integrity check failed under lock for ${runId}: ${lockedIntegrity.reason}`
          );
          return {
            ok: false,
            outcome: "patch-integrity-failure",
            reason: lockedIntegrity.reason || "patch integrity check failed",
            runId,
            patchPath,
            patchSha,
          };
        }
        return { ok: true };
      },
    });
  } finally {
    if (typeof releaseLock === "function") releaseLock();
  }
}

// --- Peer-stop integration (Task 7.4). ---

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
  // Exclusive per-(runId, target) apply lock + shared under-lock ladder with auto.
  // Concurrent dual peer-stop cannot reverse a winner; sequential restop is
  // idempotent; crash-after-apply heals the durable marker under lock.
  const patchSha = sha256File(patchPath);
  const targetKey = targetIdentityKey(repo);
  let releaseLock = null;
  try {
    releaseLock = acquireApplyLock(runId, targetKey);
  } catch (err) {
    stderrLine(`[grok-peer] BLOCKED: cannot acquire apply lock for ${runId}: ${err.message}`);
    return { attempted: true, ok: false, outcome: "blocked-apply-lock" };
  }
  try {
    const finalized = completeIntegrationApplyUnderLock({
      targetRepo: repo,
      patchPath,
      runId,
      targetKey,
      patchSha,
      env: process.env,
      stderrLine,
      logTag: "grok-peer",
      revalidateUnderLock: () => {
        // Re-verify integrity under lock (artifact may change between pre-lock check and apply).
        const lockedIntegrity = verifyPatchAgainstManifest(runId, patchPath);
        if (!lockedIntegrity.ok) {
          stderrLine(
            `[grok-peer] BLOCKED: patch integrity check failed under lock for ${runId}: ${lockedIntegrity.reason}`
          );
          return {
            ok: false,
            outcome: "patch-integrity-failure",
            reason: lockedIntegrity.reason || "patch integrity check failed",
            runId,
            patchPath,
            patchSha,
          };
        }
        return { ok: true };
      },
    });
    return { attempted: true, ok: finalized.ok, outcome: finalized.outcome };
  } finally {
    if (typeof releaseLock === "function") releaseLock();
  }
}

/**
 * maybeIntegratePeerStop wrapped to FAIL CLOSED on any unexpected throw (state-dir
 * I/O reading consent/mode, a patch vanishing between stat and hash, etc.): the
 * companion's onStdout hook must never let an apply-path exception leave a ready
 * peer-stop looking successful, so a throw becomes an attempted-but-failed
 * integration (nonzero exit + failed job).
 * @returns {{attempted: boolean, ok: boolean, outcome: string}}
 */
export function integratePeerStopFailClosed(stdout, cwd, integrationFlag, rest, stderrLine) {
  try {
    return maybeIntegratePeerStop(stdout, cwd, integrationFlag, rest, stderrLine);
  } catch (err) {
    stderrLine(`[grok-peer] integration hook error: ${err.message}`);
    return { attempted: true, ok: false, outcome: "integration-error" };
  }
}
