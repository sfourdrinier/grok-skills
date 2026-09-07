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

export function listDescendantPids(rootPid, spawnSync = nodeSpawnSync) {
  if (!Number.isInteger(rootPid) || rootPid <= 0) return [];
  const listed = spawnSync("ps", ["-ax", "-o", "pid=,ppid="], { encoding: "utf8" });
  if (!listed || listed.status !== 0) return [];
  const children = new Map();
  for (const line of String(listed.stdout || "").split("\n")) {
    const match = line.trim().match(/^(\d+)\s+(\d+)$/);
    if (!match) continue;
    const pid = Number(match[1]);
    const ppid = Number(match[2]);
    if (!children.has(ppid)) children.set(ppid, []);
    children.get(ppid).push(pid);
  }
  const out = [];
  const stack = [...(children.get(rootPid) || [])];
  const seen = new Set();
  while (stack.length) {
    const pid = stack.pop();
    if (!Number.isInteger(pid) || pid <= 1 || seen.has(pid) || pid === rootPid) continue;
    seen.add(pid);
    out.push(pid);
    for (const child of children.get(pid) || []) stack.push(child);
  }
  return out;
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

export function pidLiveness(pid, deps = {}) {
  const kill = deps.kill ?? process.kill.bind(process);
  const spawn = deps.spawnSync ?? nodeSpawnSync;
  try {
    kill(pid, 0);
  } catch (err) {
    if (err && err.code === "ESRCH") return "dead";
    return "unknown";
  }
  // kill(pid, 0) succeeds for zombies. The parent (live companion) still owns
  // the child, so cancel in another process must treat Z as observed exit.
  const listed = spawn("ps", ["-p", String(pid), "-o", "state="], { encoding: "utf8" });
  if (!listed || listed.status !== 0) return "unknown";
  const state = String(listed.stdout || "").trim().toUpperCase();
  if (!state || state.startsWith("Z")) return "dead";
  return "alive";
}

export function isPidGone(pid, deps = {}) {
  return pidLiveness(pid, deps) === "dead";
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
  const livenessOf =
    deps.pidLiveness ?? ((target) => pidLiveness(target, { kill, spawnSync: spawn }));
  const pidGone =
    deps.isPidGone ?? ((target) => livenessOf(target) === "dead");
  const waitUntilGone =
    deps.waitUntilGone ??
    ((target, ms) => defaultWaitUntilGone(target, ms, sleep, pidGone));
  const readIdentity =
    deps.readIdentity ?? ((target) => processStartIdentity(target, spawn));
  const descendantsOf =
    deps.listDescendants ?? ((target) => listDescendantPids(target, spawn));

  if (handle.startId) {
    const current = readIdentity(pid);
    if (!current) {
      return failed("could not revalidate process identity", { pid });
    }
    if (current !== handle.startId) {
      return failed("stale process identity (pid reused)", { pid });
    }
  }
  let owned = [];
  try {
    owned = descendantsOf(pid).filter((child) => Number.isInteger(child) && child > 1);
  } catch {
    owned = [];
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

  const wrapperGoneAfterTerm = waitUntilGone(pid, deps.termGraceMs ?? CANCEL_TERM_GRACE_MS);
  let wrapperGone = wrapperGoneAfterTerm;
  if (!wrapperGone) {
    try {
      kill(pid, "SIGKILL");
    } catch (err) {
      if (!(err && err.code === "ESRCH")) {
        const code = err && err.code ? String(err.code) : "";
        return failed(`${code} ${err.message}`.trim(), { pid, signaled: true });
      }
    }
    wrapperGone = waitUntilGone(pid, deps.killGraceMs ?? CANCEL_KILL_GRACE_MS);
  }
  const remaining = [];
  for (const child of owned) {
    const state = livenessOf(child);
    if (state === "dead") continue;
    if (state === "unknown") {
      remaining.push(child);
      continue;
    }
    try {
      kill(child, "SIGKILL");
    } catch (err) {
      if (!(err && err.code === "ESRCH")) remaining.push(child);
      continue;
    }
    if (livenessOf(child) !== "dead") remaining.push(child);
  }
  if (!wrapperGone || remaining.length) {
    return {
      outcome: "unconfirmed",
      observedExit: wrapperGone,
      signaled: true,
      pid,
      remaining,
      error: wrapperGone
        ? "owned descendants still live after wrapper exit"
        : "wrapper still alive after SIGTERM and SIGKILL",
    };
  }
  return { outcome: "stopped", observedExit: true, signaled: true, pid };
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
