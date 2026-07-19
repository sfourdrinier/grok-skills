// plugin/scripts/lib/integrate-apply-state.mjs
//
// Exclusive per-(runId, target) apply lock + durable applied marker keyed by
// verified patch sha + target identity. Used by integrate.mjs so concurrent
// dual peer-stop cannot reverse a winner and sequential restop is idempotent.
// Lock uses atomic mkdir (safe-state pattern; no third-party deps) with durable
// owner pid/startToken/timestamp. Automatic stale reclaim is intentionally
// disabled: Node stdlib cannot CAS an in-place lock directory without a
// generation-specific exclusive name, and rename/tombstone reclaim can displace
// a live replacement under three contenders. Abandoned locks time out with
// owner diagnostics; operator cleanup / holder release is required. Owner write
// failure removes the mkdir. Marker writes are atomic (tmp + rename) and return
// durable success.

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { runsDirFor, safeRunIdForRunsDir } from "../progress-relay.mjs";

/**
 * Age threshold for diagnostic "looks abandoned" classification only.
 * Automatic reclaim is disabled - this never authorizes deleting a lock.
 */
export const APPLY_LOCK_STALE_MS = 30_000;

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
 * Clear a non-authoritative applied marker (operator-reverted tree). Best-effort.
 * @returns {boolean}
 */
export function clearApplyMarker(runId, targetKey, env = process.env) {
  const markerPath = locateApplyMarker(runId, targetKey, env);
  if (!markerPath) return false;
  try {
    fs.unlinkSync(markerPath);
    return true;
  } catch {
    return false;
  }
}

/**
 * Write durable applied marker (private 0600) via tmp + rename. Returns false
 * when the marker is not durably on disk - callers must not claim applied.
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
    const tmp = `${markerPath}.${process.pid}.${Date.now()}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(body)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    try {
      fs.chmodSync(tmp, 0o600);
    } catch {
      /* best-effort */
    }
    fs.renameSync(tmp, markerPath);
    try {
      fs.chmodSync(markerPath, 0o600);
    } catch {
      /* best-effort */
    }
    // Re-read to prove durable presence (rename can succeed into a broken mount).
    const verify = readMatchingApplyMarker(runId, targetKey, patchSha, env);
    return verify.matched === true;
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
 * Process start identity token (pid-reuse safe). Null when unobtainable.
 * @param {number} pid
 * @returns {string|null}
 */
export function processStartToken(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  try {
    // macOS/Linux: lstart is stable for a process instance; recycled pids differ.
    const r = spawnSync("ps", ["-p", String(pid), "-o", "lstart="], {
      encoding: "utf8",
    });
    if (r.status !== 0) return null;
    const token = String(r.stdout || "").trim();
    return token || null;
  } catch {
    return null;
  }
}

/**
 * @param {number} pid
 * @returns {boolean}
 */
export function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM: process exists but we cannot signal it - treat as alive (fail closed).
    if (err && err.code === "EPERM") return true;
    return false;
  }
}

/**
 * Classify lock owner from owner.json: alive | dead | unknown.
 * @param {object|null} owner
 * @returns {"alive"|"dead"|"unknown"}
 */
export function classifyLockOwner(owner) {
  if (!owner || typeof owner !== "object") return "unknown";
  const pid = owner.pid;
  if (!Number.isInteger(pid) || pid <= 0) return "unknown";
  if (!processIsAlive(pid)) return "dead";
  const stored = owner.startToken;
  if (typeof stored === "string" && stored) {
    const current = processStartToken(pid);
    if (current != null && current !== stored) return "dead"; // pid reused
  }
  return "alive";
}

function readOwnerDoc(lockDir) {
  try {
    const doc = JSON.parse(fs.readFileSync(path.join(lockDir, "owner.json"), "utf8"));
    return doc && typeof doc === "object" ? doc : null;
  } catch {
    return null;
  }
}

/**
 * Write owner.json and re-read to prove durable presence. Throws on any failure.
 * @param {string} lockDir
 */
function writeOwnerDoc(lockDir) {
  const pid = process.pid;
  const body = {
    schemaVersion: 1,
    pid,
    startToken: processStartToken(pid),
    acquiredAt: new Date().toISOString(),
  };
  const ownerPath = path.join(lockDir, "owner.json");
  fs.writeFileSync(ownerPath, `${JSON.stringify(body)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  try {
    fs.chmodSync(ownerPath, 0o600);
  } catch {
    /* best-effort */
  }
  const verify = readOwnerDoc(lockDir);
  if (
    !verify ||
    verify.pid !== pid ||
    typeof verify.acquiredAt !== "string" ||
    !verify.acquiredAt
  ) {
    throw new Error("apply lock owner.json not durable after write");
  }
}

/**
 * Diagnostic only: true when owner identity looks positively dead and past the
 * settle age. Does **not** authorize reclaim - acquire never deletes/renames an
 * existing lock based on this signal. Unknown / ownerless / live => false.
 * @param {string} lockDir
 * @param {number} staleMs
 * @param {() => number} [nowFn]
 * @returns {boolean}
 */
export function isApplyLockReclaimable(lockDir, staleMs = APPLY_LOCK_STALE_MS, nowFn = Date.now) {
  const owner = readOwnerDoc(lockDir);
  const liveness = classifyLockOwner(owner);
  if (liveness === "alive") return false;
  if (liveness !== "dead") {
    // unknown / ownerless / unreadable: never treat as reclaimable on age alone
    return false;
  }
  let ageMs = Number.POSITIVE_INFINITY;
  const acquiredAt = owner?.acquiredAt;
  if (typeof acquiredAt === "string" && acquiredAt) {
    const t = Date.parse(acquiredAt);
    if (Number.isFinite(t)) ageMs = Math.max(0, nowFn() - t);
  } else {
    try {
      const st = fs.statSync(lockDir);
      ageMs = Math.max(0, nowFn() - st.mtimeMs);
    } catch {
      ageMs = Number.POSITIVE_INFINITY;
    }
  }
  return ageMs >= Math.min(staleMs, 1_000) || !Number.isFinite(ageMs);
}

/**
 * Automatic stale-lock reclaim is intentionally disabled.
 *
 * Rename/tombstone reclaim can still displace a live replacement under three
 * contenders (A observes dead D; B replaces; A renames B to tombstone; C takes
 * the free name; A cannot safely restore). Node stdlib has no portable CAS for
 * in-place directory ownership without a generation-specific exclusive name.
 * Correctness beats automatic reclaim: always return false; never rename/delete
 * lockDir. Abandoned locks require holder release or operator cleanup.
 *
 * @param {string} _lockDir
 * @param {number} [_staleMs]
 * @param {object} [_opts]
 * @returns {false}
 */
export function tryReclaimLockDir(_lockDir, _staleMs, _opts = {}) {
  return false;
}

/**
 * Human-readable holder diagnostics for timeout / blocked-lock messages.
 * @param {string} lockDir
 * @returns {string}
 */
export function formatApplyLockDiag(lockDir) {
  const owner = readOwnerDoc(lockDir);
  if (!owner) {
    return (
      "holder=unknown/unreadable; automatic reclaim disabled - " +
      "release the holder or remove the lock dir after confirming it is abandoned"
    );
  }
  const liveness = classifyLockOwner(owner);
  const pid = Number.isInteger(owner.pid) ? owner.pid : "?";
  const acquiredAt =
    typeof owner.acquiredAt === "string" && owner.acquiredAt ? owner.acquiredAt : "?";
  const abandoned = isApplyLockReclaimable(lockDir) ? "yes" : "no";
  return (
    `holder liveness=${liveness} pid=${pid} acquiredAt=${acquiredAt} ` +
    `looksAbandoned=${abandoned}; automatic reclaim disabled - ` +
    "release the holder or remove the lock dir after confirming it is abandoned"
  );
}

function removeLockDirBestEffort(lockDir) {
  try {
    fs.rmSync(lockDir, { recursive: true, force: true });
  } catch {
    try {
      fs.rmdirSync(lockDir);
    } catch {
      /* best-effort */
    }
  }
}

/**
 * Exclusive per-(runId, target) apply lock via atomic mkdir + durable owner record.
 * Returns release() or throws on timeout / owner-write failure. Existing locks
 * (live, dead, or unknown) are **never** reclaimed in place - acquire waits for
 * holder release or times out with owner diagnostics. If mkdir succeeds but
 * owner.json cannot be written and re-read, the lock dir is removed and acquire
 * fails closed. `opts.staleMs` is accepted for API compatibility but unused
 * (reclaim is disabled).
 *
 * @param {string} runId
 * @param {string} targetKey
 * @param {NodeJS.ProcessEnv} [env]
 * @param {number} [timeoutMs]
 * @param {{staleMs?: number}} [opts]
 * @returns {() => void}
 */
export function acquireApplyLock(
  runId,
  targetKey,
  env = process.env,
  timeoutMs = 30_000,
  opts = {}
) {
  void opts; // staleMs retained for callers; automatic reclaim is disabled
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
    } catch (err) {
      if (!err || err.code !== "EEXIST") throw err;
      // Never reclaim / rename / delete an existing lock dir. Wait or timeout.
      sleepMs(15);
      continue;
    }
    try {
      writeOwnerDoc(lockDir);
    } catch (err) {
      // Fail closed: never leave an ownerless lock behind.
      removeLockDirBestEffort(lockDir);
      const detail = err && err.message ? String(err.message) : "owner write failed";
      throw new Error(
        `apply lock owner write failed (fail closed, lock removed): ${detail}`
      );
    }
    return () => {
      removeLockDirBestEffort(lockDir);
    };
  }
  throw new Error(
    `apply lock timeout for ${safe}/${targetKey} (${formatApplyLockDiag(lockDir)})`
  );
}
