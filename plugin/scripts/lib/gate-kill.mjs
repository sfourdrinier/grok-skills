// plugin/scripts/lib/gate-kill.mjs
//
// F-GATE-ORPHAN / F1 / F3: terminate the whole review process tree on the gate's
// own timeout WITHOUT ever signaling the caller's own process group. Split out of
// stop-review-gate-hook.mjs so the pid guard and the kill sequence are unit
// testable -- the hook itself runs main() at import time and cannot be imported.

import { spawnSync as nodeSpawnSync } from "node:child_process";
import process from "node:process";

// After SIGTERM-ing the review process group we give the python wrapper a brief
// window to run its OWN SIGTERM handler (which tears down the grok CLI it spawned
// in a separate session and runs private-home cleanup) before we SIGKILL the
// group to guarantee nothing survives and the gate cannot hang.
export const GROUP_TERM_GRACE_MS = 500;

/**
 * Resolve the group-killable child pid from a spawnSync result, or null.
 *
 * F1: a FAILED spawn yields pid 0 (or undefined), and `process.kill(-0, ...)`
 * signals the CALLER's OWN process group -- which can take down the harness. Only
 * a positive child pid is safe to negate for a process-group kill.
 *
 * @param {{ pid?: number }} result
 * @returns {number | null}
 */
export function resolveSpawnedGroupPid(result) {
  const pid = result ? result.pid : undefined;
  return Number.isInteger(pid) && pid > 0 ? pid : null;
}

/**
 * Block the current thread for `ms` without an event-loop tick. Used only during
 * timeout teardown (the gate has already timed out, so a short synchronous pause
 * is harmless) to let the python wrapper's SIGTERM handler run before SIGKILL.
 * @param {number} ms
 */
export function sleepSync(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch (err) {
    // SharedArrayBuffer/Atomics unavailable (unusual). The follow-up SIGKILL
    // still runs, just without the graceful window. Never throw from teardown.
    process.stderr.write(`[grok-stop-gate] synchronous grace unavailable during teardown: ${err.message}\n`);
  }
}

function signalGroup(kill, pid, signalName) {
  try {
    kill(-pid, signalName);
  } catch (err) {
    if (err && err.code !== "ESRCH") {
      process.stderr.write(`[grok-stop-gate] could not ${signalName} review tree group ${pid}: ${err.message}\n`);
    }
  }
}

/**
 * Terminate the ENTIRE review process tree for a positive child pid, so the
 * companion Node process, the python wrapper, and the grok CLI it spawned (in its
 * own session) are all torn down instead of orphaned.
 *   - POSIX: SIGTERM the process GROUP first (so the python wrapper's SIGTERM
 *     handler kills the grok CLI and runs private-home cleanup), then, after a
 *     short grace, SIGKILL the group so nothing survives and the gate cannot hang.
 *   - Windows: no process groups here, so kill the whole tree by pid with
 *     `taskkill /T /F` (which the earlier per-child kill did NOT do), reaching the
 *     wrapper and grok grandchild.
 *
 * @param {number} pid a POSITIVE child pid (caller resolves via resolveSpawnedGroupPid)
 * @param {boolean} isPosix
 * @param {{ kill?: Function, spawnSync?: Function, sleep?: Function }} [deps] injectable for tests
 */
export function terminateReviewTree(pid, isPosix, deps = {}) {
  const kill = deps.kill ?? process.kill.bind(process);
  const spawn = deps.spawnSync ?? nodeSpawnSync;
  const sleep = deps.sleep ?? sleepSync;

  if (!isPosix) {
    // Capture stderr (not stdio:"ignore") and inspect BOTH the spawn error AND a
    // nonzero exit status: spawnSync only sets `.error` for a failed spawn, so a
    // taskkill that RAN but failed its job (e.g. exit 128 "access denied" against
    // a permission-restricted grandchild, or a stale/reused PID) would otherwise
    // be a silent termination failure (Round4 F3). Surface taskkill's own stderr.
    const killed = spawn("taskkill", ["/T", "/F", "/PID", String(pid)], { encoding: "utf8" });
    if (killed && killed.error) {
      process.stderr.write(
        `[grok-stop-gate] taskkill could not be spawned for review tree pid ${pid}: ${killed.error.message}\n`
      );
    } else if (killed && typeof killed.status === "number" && killed.status !== 0) {
      const detail = (killed.stderr || "").toString().trim() || `exit status ${killed.status}`;
      process.stderr.write(
        `[grok-stop-gate] taskkill FAILED to kill review tree pid ${pid} (exit ${killed.status}): ${detail}\n`
      );
    }
    return;
  }

  signalGroup(kill, pid, "SIGTERM");
  sleep(GROUP_TERM_GRACE_MS);
  signalGroup(kill, pid, "SIGKILL");
}
