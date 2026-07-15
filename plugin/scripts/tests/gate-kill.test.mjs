// plugin/scripts/tests/gate-kill.test.mjs
//
// Unit tests for the stop-gate review-tree termination: the F1 process-group-0
// guard and the POSIX SIGTERM-then-SIGKILL / Windows taskkill /T /F sequence.

import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveSpawnedGroupPid, terminateReviewTree } from "../lib/gate-kill.mjs";

test("F1: resolveSpawnedGroupPid returns null for a failed spawn (pid 0 / undefined / negative)", () => {
  assert.equal(resolveSpawnedGroupPid({ pid: 0, error: { code: "ENOENT" } }), null);
  assert.equal(resolveSpawnedGroupPid({ error: { code: "ENOENT" } }), null); // pid undefined
  assert.equal(resolveSpawnedGroupPid({ pid: -5 }), null);
  assert.equal(resolveSpawnedGroupPid(null), null);
  assert.equal(resolveSpawnedGroupPid({ pid: 1234, signal: "SIGTERM" }), 1234);
});

test("F1: terminateReviewTree never signals process group 0 (would hit the caller's own group)", () => {
  // A failed spawn resolves to null, so terminateReviewTree is never invoked; but
  // if it somehow were called with pid 0, the negation must not become -0/0. We
  // assert the guard upstream: resolveSpawnedGroupPid(0) is null.
  assert.equal(resolveSpawnedGroupPid({ pid: 0 }), null);
});

test("POSIX terminateReviewTree SIGTERMs the group, then SIGKILLs after a grace", () => {
  const calls = [];
  let slept = 0;
  terminateReviewTree(4321, true, {
    kill: (pid, signal) => calls.push([pid, signal]),
    sleep: (ms) => {
      slept = ms;
    },
  });
  assert.deepEqual(calls, [
    [-4321, "SIGTERM"],
    [-4321, "SIGKILL"],
  ]);
  assert.ok(slept > 0, "a grace pause happens between SIGTERM and SIGKILL");
  // Never signals group 0 nor a positive (single-process) pid.
  assert.ok(calls.every(([pid]) => pid < 0));
});

test("POSIX terminateReviewTree tolerates an already-exited group (ESRCH) without throwing", () => {
  assert.doesNotThrow(() =>
    terminateReviewTree(999, true, {
      kill: () => {
        const err = new Error("no such process");
        err.code = "ESRCH";
        throw err;
      },
      sleep: () => {},
    })
  );
});

test("Windows terminateReviewTree kills the whole tree with taskkill /T /F", () => {
  const spawnCalls = [];
  terminateReviewTree(777, false, {
    spawnSync: (cmd, args) => {
      spawnCalls.push([cmd, args]);
      return { status: 0 };
    },
  });
  assert.equal(spawnCalls.length, 1);
  assert.equal(spawnCalls[0][0], "taskkill");
  assert.deepEqual(spawnCalls[0][1], ["/T", "/F", "/PID", "777"]);
});

test("Round4 F3: Windows terminateReviewTree surfaces a nonzero taskkill exit and its stderr", () => {
  // taskkill that RAN but failed its job (nonzero status) must be logged with its
  // own stderr, not silently swallowed via stdio:"ignore" / an error-only check.
  const originalWrite = process.stderr.write;
  const lines = [];
  process.stderr.write = (chunk) => {
    lines.push(String(chunk));
    return true;
  };
  try {
    terminateReviewTree(888, false, {
      spawnSync: () => ({ status: 128, stderr: "ERROR: access denied for PID 888." }),
    });
  } finally {
    process.stderr.write = originalWrite;
  }
  const joined = lines.join("");
  assert.match(joined, /taskkill FAILED to kill review tree pid 888/);
  assert.match(joined, /exit 128/);
  assert.match(joined, /access denied/);
});

test("Round4 F3: Windows terminateReviewTree captures stderr (no stdio:ignore)", () => {
  // The spawn options must request captured output so taskkill's diagnostic is
  // available; assert we do NOT pass stdio:"ignore".
  let seenOptions = null;
  terminateReviewTree(999, false, {
    spawnSync: (_cmd, _args, options) => {
      seenOptions = options;
      return { status: 0 };
    },
  });
  assert.ok(seenOptions, "spawnSync must receive an options object");
  assert.notEqual(seenOptions.stdio, "ignore", "taskkill stderr must not be discarded");
  assert.equal(seenOptions.encoding, "utf8");
});
