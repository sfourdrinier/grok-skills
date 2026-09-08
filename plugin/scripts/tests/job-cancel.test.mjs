// plugin/scripts/tests/job-cancel.test.mjs
//
// F01: job cancel must signal the wrapper pid (not a guessed process group),
// wait until owned execution has stopped, and only then mark cancelled.

import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { createJob, getJob, updateJob } from "../lib/jobs.mjs";
import {
  cancelTrackedJob,
  isPidGone,
  pidLiveness,
  stopOwnedWrapper,
} from "../lib/job-cancel.mjs";

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "cancel-tree.py"
);

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test("pidLiveness treats a failed ps after kill(0) as unknown, not dead", () => {
  const liveness = pidLiveness(4242, {
    kill: () => {},
    spawnSync: () => ({ status: 127, stdout: "", stderr: "ps: not found" }),
  });
  assert.equal(liveness, "unknown");
  assert.equal(
    isPidGone(4242, {
      kill: () => {},
      spawnSync: () => ({ status: 127, stdout: "", stderr: "ps: not found" }),
    }),
    false
  );
});

test("unreadable start identity is failed, not signalled", () => {
  const calls = [];
  const result = stopOwnedWrapper({ pid: 21, startId: "21:boot" }, true, {
    kill: (pid, signal) => calls.push([pid, signal]),
    readIdentity: () => null,
    waitUntilGone: () => false,
    sleep: () => {},
  });
  assert.equal(result.outcome, "failed");
  assert.equal(calls.length, 0);
  assert.match(String(result.error), /identity|unknown|revalidate/i);
});

test("forced stop is unconfirmed when a descendant stays live after wrapper exit", () => {
  const killed = [];
  const result = stopOwnedWrapper({ pid: 31, startId: "31:a" }, true, {
    kill: (pid, signal) => {
      killed.push([pid, signal]);
    },
    readIdentity: () => "31:a",
    waitUntilGone: (pid) => pid === 31,
    listDescendants: () => [99],
    pidLiveness: (pid) => (pid === 31 ? "dead" : "alive"),
    sleep: () => {},
  });
  assert.equal(result.outcome, "unconfirmed");
  assert.equal(result.observedExit, true);
  assert.ok(killed.some(([pid, signal]) => pid === 99 && signal === "SIGKILL"));
});

test("isPidGone treats a zombie as observed exit", { skip: process.platform === "win32" }, () => {
  const child = spawn("python3", ["-c", "import time; time.sleep(30)"], {
    stdio: "ignore",
  });
  assert.ok(child.pid > 0);
  process.kill(child.pid, "SIGKILL");
  const deadline = Date.now() + 2000;
  let gone = false;
  while (Date.now() < deadline) {
    gone = isPidGone(child.pid);
    if (gone) break;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
  }
  assert.equal(gone, true, "zombie must count as gone before the parent reaps");
  child.kill("SIGKILL");
});

test("POSIX stopOwnedWrapper SIGTERMs the wrapper pid, not a process group", () => {
  const calls = [];
  const result = stopOwnedWrapper(
    { pid: 4321, startId: "4321:boot" },
    true,
    {
      kill: (pid, signal) => calls.push([pid, signal]),
      readIdentity: () => "4321:boot",
      waitUntilGone: () => true,
      sleep: () => {},
    }
  );
  assert.equal(result.outcome, "stopped");
  assert.equal(result.observedExit, true);
  assert.deepEqual(calls[0], [4321, "SIGTERM"]);
  assert.ok(calls.every(([pid]) => pid > 0), "must not negate pid for group kill");
});

test("POSIX stopOwnedWrapper does not SIGKILL if SIGTERM reaps the wrapper", () => {
  const calls = [];
  stopOwnedWrapper({ pid: 7, startId: "7:a" }, true, {
    kill: (pid, signal) => calls.push([pid, signal]),
    readIdentity: () => "7:a",
    waitUntilGone: () => true,
    sleep: () => {},
  });
  assert.deepEqual(calls, [[7, "SIGTERM"]]);
});

test("POSIX permission failure is failed, not cancelled", () => {
  const result = stopOwnedWrapper({ pid: 9, startId: "9:a" }, true, {
    kill: () => {
      const err = new Error("operation not permitted");
      err.code = "EPERM";
      throw err;
    },
    readIdentity: () => "9:a",
    waitUntilGone: () => false,
    sleep: () => {},
  });
  assert.equal(result.outcome, "failed");
  assert.equal(result.observedExit, false);
  assert.match(String(result.error), /EPERM|not permitted/);
});

test("POSIX ESRCH on SIGTERM is already_dead", () => {
  const result = stopOwnedWrapper({ pid: 11, startId: "11:a" }, true, {
    kill: () => {
      const err = new Error("no such process");
      err.code = "ESRCH";
      throw err;
    },
    readIdentity: () => "11:a",
    waitUntilGone: () => true,
    sleep: () => {},
  });
  assert.equal(result.outcome, "already_dead");
  assert.equal(result.observedExit, true);
});

test("stale start identity refuses to signal", () => {
  const calls = [];
  const result = stopOwnedWrapper({ pid: 13, startId: "13:old" }, true, {
    kill: (pid, signal) => calls.push([pid, signal]),
    readIdentity: () => "13:reused",
    waitUntilGone: () => false,
    sleep: () => {},
  });
  assert.equal(result.outcome, "failed");
  assert.equal(calls.length, 0);
  assert.match(String(result.error), /identity|stale|reused/i);
});

test("wait timeout after SIGKILL is unconfirmed", () => {
  const result = stopOwnedWrapper({ pid: 15, startId: "15:a" }, true, {
    kill: () => {},
    readIdentity: () => "15:a",
    waitUntilGone: () => false,
    sleep: () => {},
  });
  assert.equal(result.outcome, "unconfirmed");
  assert.equal(result.observedExit, false);
});

test("Windows nonzero taskkill is failed, not stopped", () => {
  const result = stopOwnedWrapper({ pid: 888, startId: "888:a" }, false, {
    spawnSync: () => ({ status: 128, stderr: "ERROR: access denied for PID 888." }),
    readIdentity: () => "888:a",
    waitUntilGone: () => false,
    sleep: () => {},
  });
  assert.equal(result.outcome, "failed");
  assert.equal(result.observedExit, false);
  assert.match(String(result.error), /128|access denied/);
});

test("cancelTrackedJob is a no-op for an already finished job", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-done-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(cwd, { kind: "run", mode: "code", runMode: "hardened" }, env);
  updateJob(cwd, job.id, { status: "success", summary: "completed", pid: 4242 }, env);
  const calls = [];
  const out = cancelTrackedJob(cwd, getJob(cwd, job.id, env), env, {
    stop: (handle) => {
      calls.push(handle);
      return { outcome: "stopped", observedExit: true, signaled: true };
    },
  });
  assert.equal(out.code, 0);
  assert.equal(out.status, "success");
  assert.equal(calls.length, 0);
  assert.equal(getJob(cwd, job.id, env).status, "success");
});

test("cancelTrackedJob with no pid is cancel_failed, not cancelled", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-nopid-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(cwd, { kind: "run", mode: "code", runMode: "hardened" }, env);
  const out = cancelTrackedJob(cwd, job, env, {
    stop: () => {
      throw new Error("should not stop");
    },
  });
  assert.equal(out.code, 1);
  assert.equal(out.status, "cancel_failed");
  assert.equal(getJob(cwd, job.id, env).status, "cancel_failed");
});

test("cancelTrackedJob marks cancelled only after observed stop", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-ok-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(
    cwd,
    { kind: "run", mode: "code", runMode: "hardened", pid: 99, startId: "99:a" },
    env
  );
  const out = cancelTrackedJob(cwd, job, env, {
    stop: () => ({ outcome: "stopped", observedExit: true, signaled: true, pid: 99 }),
  });
  assert.equal(out.code, 0);
  assert.equal(out.status, "cancelled");
  assert.equal(getJob(cwd, job.id, env).status, "cancelled");
});

test("cancelTrackedJob stop failure does not relabel the job cancelled", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-fail-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(
    cwd,
    { kind: "run", mode: "code", runMode: "hardened", pid: 101, startId: "101:a" },
    env
  );
  const out = cancelTrackedJob(cwd, job, env, {
    stop: () => ({
      outcome: "failed",
      observedExit: false,
      signaled: false,
      error: "EPERM",
      pid: 101,
    }),
  });
  assert.equal(out.code, 1);
  assert.equal(out.status, "cancel_failed");
  assert.notEqual(getJob(cwd, job.id, env).status, "cancelled");
});

test("cancelTrackedJob unconfirmed stop does not claim cancelled", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-unconf-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(
    cwd,
    { kind: "run", mode: "code", runMode: "hardened", pid: 103, startId: "103:a" },
    env
  );
  const out = cancelTrackedJob(cwd, job, env, {
    stop: () => ({
      outcome: "unconfirmed",
      observedExit: false,
      signaled: true,
      pid: 103,
    }),
  });
  assert.equal(out.code, 1);
  assert.equal(out.status, "cancel_unconfirmed");
  assert.notEqual(getJob(cwd, job.id, env).status, "cancelled");
});

test(
  "real fixture tree: SIGTERM wrapper pid exits parent and owned child; caller lives",
  { skip: process.platform === "win32" },
  async () => {
    const spawned = spawn("python3", [FIXTURE], { stdio: ["ignore", "pipe", "pipe"] });
    const line = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("fixture stdout timeout")), 5000);
      let buf = "";
      spawned.stdout.on("data", (chunk) => {
        buf += chunk;
        if (buf.includes("\n")) {
          clearTimeout(timer);
          resolve(buf.trim().split("\n")[0]);
        }
      });
      spawned.on("error", reject);
    });
    const [parentPid, childPid] = line.split(" ").map((n) => Number(n));
    assert.ok(parentPid > 0 && childPid > 0);
    assert.equal(spawned.pid, parentPid);
    assert.ok(alive(parentPid));
    assert.ok(alive(childPid));
    const callerPid = process.pid;

    const result = stopOwnedWrapper(
      { pid: parentPid, startId: null },
      true,
      { identityGrace: true }
    );
    assert.ok(["stopped", "already_dead"].includes(result.outcome), result.error);
    assert.equal(result.observedExit, true);
    assert.equal(isPidGone(parentPid), true, "wrapper parent must exit (zombie counts)");
    const childDeadline = Date.now() + 2000;
    let childGone = isPidGone(childPid);
    while (!childGone && Date.now() < childDeadline) {
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
      childGone = isPidGone(childPid);
    }
    assert.equal(childGone, true, "owned child must exit");
    assert.ok(alive(callerPid), "caller must remain alive");
    try {
      spawned.kill("SIGKILL");
    } catch {
      /* already gone */
    }
  }
);

test("updateJob does not relabel a cancelled job as success", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-sticky-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, ".pdata") };
  const job = createJob(cwd, { kind: "run", mode: "code", runMode: "hardened" }, env);
  updateJob(cwd, job.id, { status: "cancelled", summary: "cancelled" }, env);
  const after = updateJob(cwd, job.id, { status: "success", summary: "completed" }, env);
  assert.equal(after.status, "cancelled");
  fs.rmSync(cwd, { recursive: true, force: true });
});

test("createJob records startId when pid is supplied", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-handle-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(
    cwd,
    {
      kind: "run",
      mode: "code",
      pid: 55,
      pgid: null,
      pgidKind: "none",
      startId: "55:boot",
    },
    env
  );
  assert.equal(job.pid, 55);
  assert.equal(job.pgidKind, "none");
  assert.equal(job.startId, "55:boot");
});
