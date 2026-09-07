// plugin/scripts/lib/job-cancel.mjs
//
// F01: stop an owned companion job by signalling the wrapper pid and waiting
// for observed exit. Stop-review-gate group teardown stays in gate-kill.mjs.

import { spawnSync as nodeSpawnSync } from "node:child_process";
import process from "node:process";

import { updateJob } from "./jobs.mjs";

export const CANCEL_TERM_GRACE_MS = 2000;
export const CANCEL_KILL_GRACE_MS = 1000;

const FINISHED = new Set(["success", "failure", "cancelled"]);

export function isFinishedJobStatus(status) {
  return FINISHED.has(status);
}

export function processStartIdentity(pid, spawnSync = nodeSpawnSync) {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const result = spawnSync("ps", ["-p", String(pid), "-o", "lstart="], {
    encoding: "utf8",
  });
  if (result.status !== 0) return null;
  const start = String(result.stdout || "").trim();
  if (!start) return null;
  return `${pid}:${start}`;
}

function defaultSleep(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      /* spin */
    }
  }
}

export function isPidGone(pid, deps = {}) {
  const kill = deps.kill ?? process.kill.bind(process);
  const spawn = deps.spawnSync ?? nodeSpawnSync;
  try {
    kill(pid, 0);
  } catch (err) {
    return Boolean(err && err.code === "ESRCH");
  }
  // kill(pid, 0) succeeds for zombies. The parent (live companion) still owns
  // the child, so cancel in another process must treat Z as observed exit.
  const listed = spawn("ps", ["-p", String(pid), "-o", "state="], { encoding: "utf8" });
  if (listed.status !== 0) return true;
  const state = String(listed.stdout || "").trim().toUpperCase();
  return !state || state.startsWith("Z");
}

function defaultWaitUntilGone(pid, timeoutMs, sleep, gone) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() <= deadline) {
    if (gone(pid)) return true;
    sleep(50);
  }
  return gone(pid);
}

function failed(error, extra = {}) {
  return {
    outcome: "failed",
    observedExit: false,
    signaled: false,
    error,
    ...extra,
  };
}

export function stopOwnedWrapper(handle, isPosix, deps = {}) {
  const pid = handle?.pid;
  if (!Number.isInteger(pid) || pid <= 0) {
    return failed("no live wrapper pid");
  }
  const kill = deps.kill ?? process.kill.bind(process);
  const spawn = deps.spawnSync ?? nodeSpawnSync;
  const sleep = deps.sleep ?? defaultSleep;
  const pidGone =
    deps.isPidGone ?? ((target) => isPidGone(target, { kill, spawnSync: spawn }));
  const waitUntilGone =
    deps.waitUntilGone ??
    ((target, ms) => defaultWaitUntilGone(target, ms, sleep, pidGone));
  const readIdentity =
    deps.readIdentity ?? ((target) => processStartIdentity(target, spawn));

  if (handle.startId) {
    const current = readIdentity(pid);
    if (current && current !== handle.startId) {
      return failed("stale process identity (pid reused)", { pid });
    }
  }

  if (!isPosix) {
    const killed = spawn("taskkill", ["/T", "/F", "/PID", String(pid)], {
      encoding: "utf8",
    });
    if (killed && killed.error) {
      return failed(killed.error.message, { pid });
    }
    if (killed && typeof killed.status === "number" && killed.status !== 0) {
      const detail =
        (killed.stderr || "").toString().trim() || `exit status ${killed.status}`;
      return failed(detail, { pid, signaled: true });
    }
    const exited = waitUntilGone(pid, deps.killGraceMs ?? CANCEL_KILL_GRACE_MS);
    return {
      outcome: exited ? "stopped" : "unconfirmed",
      observedExit: exited,
      signaled: true,
      pid,
    };
  }

  try {
    kill(pid, "SIGTERM");
  } catch (err) {
    if (err && err.code === "ESRCH") {
      return { outcome: "already_dead", observedExit: true, signaled: false, pid };
    }
    const code = err && err.code ? String(err.code) : "";
    return failed(`${code} ${err.message}`.trim(), { pid });
  }

  if (waitUntilGone(pid, deps.termGraceMs ?? CANCEL_TERM_GRACE_MS)) {
    return { outcome: "stopped", observedExit: true, signaled: true, pid };
  }

  try {
    kill(pid, "SIGKILL");
  } catch (err) {
    if (err && err.code === "ESRCH") {
      return { outcome: "stopped", observedExit: true, signaled: true, pid };
    }
    const code = err && err.code ? String(err.code) : "";
    return failed(`${code} ${err.message}`.trim(), { pid, signaled: true });
  }

  const exited = waitUntilGone(pid, deps.killGraceMs ?? CANCEL_KILL_GRACE_MS);
  if (exited) {
    return { outcome: "stopped", observedExit: true, signaled: true, pid };
  }
  return {
    outcome: "unconfirmed",
    observedExit: false,
    signaled: true,
    pid,
    error: "wrapper still alive after SIGTERM and SIGKILL",
  };
}

export function cancelTrackedJob(cwd, job, env = process.env, deps = {}) {
  if (!job) {
    return { code: 1, status: null, message: "no job to cancel" };
  }
  if (isFinishedJobStatus(job.status)) {
    return {
      code: 0,
      status: job.status,
      message: `Job ${job.id} is already ${job.status}.`,
    };
  }
  const pid = job.pid;
  if (!Number.isInteger(pid) || pid <= 0) {
    const updated = updateJob(
      cwd,
      job.id,
      {
        status: "cancel_failed",
        summary: "cancel failed: no pid recorded; process may still be running",
      },
      env
    );
    return {
      code: 1,
      status: updated.status,
      message:
        `Job ${job.id}: no live pid recorded; not marked cancelled (process may still be running).`,
    };
  }

  updateJob(cwd, job.id, { status: "cancel_requested", summary: "cancel requested" }, env);

  const isPosix = process.platform !== "win32";
  const stop =
    deps.stop ??
    ((handle, posix, stopDeps) => stopOwnedWrapper(handle, posix, stopDeps));
  const result = stop(
    {
      pid,
      startId: job.startId ?? null,
      pgid: job.pgid ?? null,
      pgidKind: job.pgidKind ?? "none",
      runId: job.runId ?? null,
    },
    isPosix,
    deps
  );

  if (result.outcome === "stopped" || result.outcome === "already_dead") {
    const updated = updateJob(
      cwd,
      job.id,
      { status: "cancelled", summary: "cancelled by operator" },
      env
    );
    return {
      code: 0,
      status: updated.status,
      result,
      message: `Cancelled job ${job.id} (wrapper pid ${pid}).`,
    };
  }

  const status = result.outcome === "unconfirmed" ? "cancel_unconfirmed" : "cancel_failed";
  const updated = updateJob(
    cwd,
    job.id,
    {
      status,
      summary: `cancel ${result.outcome}: ${result.error || result.outcome}`,
    },
    env
  );
  return {
    code: 1,
    status: updated.status,
    result,
    message: `Job ${job.id}: cancel ${result.outcome} for pid ${pid}${
      result.error ? `: ${result.error}` : ""
    }.`,
  };
}
