// plugin/scripts/lib/integrate-apply-state.mjs
//
// Exclusive per-(runId, target) apply lock + durable applied marker keyed by
// verified patch sha + target identity. Used by integrate.mjs so concurrent
// dual peer-stop cannot reverse a winner and sequential restop is idempotent.
// Lock uses atomic mkdir (safe-state pattern; no third-party deps).

import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { runsDirFor, safeRunIdForRunsDir } from "../progress-relay.mjs";

/**
 * Stable short identity for a target workspace (realpath hash).
 * @param {string} targetRepo
 * @returns {string}
 */
export function targetIdentityKey(targetRepo) {
  let abs = path.resolve(String(targetRepo || "."));
  try {
    abs = fs.realpathSync.native(abs);
  } catch {
    /* logical path */
  }
  return createHash("sha256").update(abs).digest("hex").slice(0, 16);
}

/**
 * Durable apply-outcome marker path for (runId, targetKey).
 * @param {string} runId
 * @param {string} targetKey
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null}
 */
export function locateApplyMarker(runId, targetKey, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe || !targetKey) return null;
  return path.join(runsDir, safe, `integration-applied-${targetKey}.json`);
}

/**
 * @param {string} runId
 * @param {string} targetKey
 * @param {string} patchSha
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {{matched: boolean, marker: object|null}}
 */
export function readMatchingApplyMarker(runId, targetKey, patchSha, env = process.env) {
  const markerPath = locateApplyMarker(runId, targetKey, env);
  if (!markerPath) return { matched: false, marker: null };
  try {
    const doc = JSON.parse(fs.readFileSync(markerPath, "utf8"));
    if (
      doc &&
      typeof doc === "object" &&
      doc.outcome === "applied" &&
      typeof doc.patchSha === "string" &&
      doc.patchSha.toLowerCase() === String(patchSha || "").toLowerCase() &&
      doc.targetKey === targetKey
    ) {
      return { matched: true, marker: doc };
    }
    return { matched: false, marker: doc };
  } catch {
    return { matched: false, marker: null };
  }
}

/**
 * Write durable applied marker (private 0600). Best-effort; never throws out.
 * @returns {boolean}
 */
export function writeApplyMarker(runId, targetKey, patchSha, env = process.env) {
  const markerPath = locateApplyMarker(runId, targetKey, env);
  if (!markerPath) return false;
  try {
    fs.mkdirSync(path.dirname(markerPath), { recursive: true, mode: 0o700 });
    const body = {
      outcome: "applied",
      patchSha: String(patchSha || "").toLowerCase(),
      targetKey,
      appliedAt: new Date().toISOString(),
    };
    fs.writeFileSync(markerPath, `${JSON.stringify(body)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    try {
      fs.chmodSync(markerPath, 0o600);
    } catch {
      /* best-effort */
    }
    return true;
  } catch {
    return false;
  }
}

function sleepMs(ms) {
  const sab = new SharedArrayBuffer(4);
  const view = new Int32Array(sab);
  Atomics.wait(view, 0, 0, Math.max(1, ms | 0));
}

/**
 * Exclusive per-(runId, target) apply lock via atomic mkdir (existing safe-state
 * pattern; no third-party deps). Returns release() or throws on timeout.
 * @param {string} runId
 * @param {string} targetKey
 * @param {NodeJS.ProcessEnv} [env]
 * @param {number} [timeoutMs]
 * @returns {() => void}
 */
export function acquireApplyLock(runId, targetKey, env = process.env, timeoutMs = 30_000) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe || !targetKey) {
    throw new Error("apply lock requires safe runId and targetKey");
  }
  const lockDir = path.join(runsDir, safe, "apply-locks", `${targetKey}.lock`);
  fs.mkdirSync(path.dirname(lockDir), { recursive: true, mode: 0o700 });
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      fs.mkdirSync(lockDir);
      return () => {
        try {
          fs.rmdirSync(lockDir);
        } catch {
          /* best-effort release */
        }
      };
    } catch (err) {
      if (!err || err.code !== "EEXIST") throw err;
      sleepMs(15);
    }
  }
  throw new Error(`apply lock timeout for ${safe}/${targetKey}`);
}
