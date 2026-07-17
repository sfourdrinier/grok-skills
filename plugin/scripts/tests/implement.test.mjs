// plugin/scripts/tests/implement.test.mjs
//
// Task 1.4: implement combo mode (code + auto-handoff, ready-gated exit).
// Fake-wrapper only - never spawns the real wrapper or Grok CLI.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { DIRECT_NO_HANDOFF_MSG } from "../lib/direct-grok.mjs";
import { setRunMode } from "../lib/jobs.mjs";
import { runImplementCombo } from "../lib/implement.mjs";
import { makeFakeWrapper, readCalls, runCompanion } from "./helpers/fake-wrapper.mjs";

const RUN_ID = "20260716T000000Z-abc123";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-implement-"));
}

function codeEnvelope(overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: "success",
    mode: "code",
    runId: RUN_ID,
    response: { text: "code-done" },
    ...overrides,
  });
}

function handoffEnvelope(ready, overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: ready ? "success" : "failure",
    mode: "handoff",
    runId: RUN_ID,
    response: {
      integration: { ready, blockers: ready ? [] : [{ kind: "handoff-unavailable" }] },
    },
    ...overrides,
  });
}

function companionEnv(env, cwd, callsPath) {
  return {
    ...env,
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata"),
    GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
    ...(callsPath ? { FAKE_WRAPPER_CALLS: callsPath } : {}),
  };
}

test("implement runs code then handoff and exits 0 only when ready", () => {
  const cwd = tempCwd();
  const codeOut = codeEnvelope();
  const handoffOut = handoffEnvelope(true);
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeOut}\n`, exitCode: 0 },
    handoff: { stdout: `${handoffOut}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "implement",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "fix it",
      ],
      { cwd, env: companionEnv(env, cwd, callsPath) }
    );
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    const codeIdx = res.stdout.indexOf(codeOut);
    const handoffIdx = res.stdout.indexOf(handoffOut);
    assert.ok(codeIdx >= 0, `missing code envelope: ${res.stdout}`);
    assert.ok(handoffIdx >= 0, `missing handoff envelope: ${res.stdout}`);
    assert.ok(codeIdx < handoffIdx, "code envelope must precede handoff envelope");
    // Real handoff ready path: response.integration.ready
    assert.match(res.stdout, /"integration"\s*:\s*\{\s*"ready"\s*:\s*true/);
    assert.deepEqual(readCalls(callsPath), ["code", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("implement exits 1 when handoff not ready", () => {
  const cwd = tempCwd();
  const codeOut = codeEnvelope();
  const handoffOut = handoffEnvelope(false);
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeOut}\n`, exitCode: 0 },
    handoff: { stdout: `${handoffOut}\n`, exitCode: 1 },
  });
  try {
    const res = runCompanion(
      [
        "implement",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "fix it",
      ],
      { cwd, env: companionEnv(env, cwd, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.ok(res.stdout.includes(codeOut), "code envelope must still be relayed");
    assert.ok(res.stdout.includes(handoffOut), "handoff envelope must still be relayed");
    assert.deepEqual(readCalls(callsPath), ["code", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("C6: code leg is skipNotify; finalizeCombo fires once with the true outcome", async () => {
  // No runId in the code envelope -> runCodeThenHandoff returns before any
  // handoff spawn, so this unit-tests the finalize wiring without a subprocess.
  const track = { kind: "code", mode: "code", notifyMode: "implement", runMode: "hardened" };
  let relayTrack = null;
  const fakeRelay = async (_wrapper, _args, t) => {
    relayTrack = t;
    return {
      code: 0,
      stdout: JSON.stringify({ schemaVersion: 1, status: "success", mode: "code" }),
      jobId: "job-1",
    };
  };
  const finalizeCalls = [];
  const exit = await runImplementCombo("wrapper", ["--target", ".", "--base", "HEAD"], "hardened", track, {
    runWithLiveRelay: fakeRelay,
    stderrLine: () => {},
    finalizeCombo: async (x) => finalizeCalls.push(x),
  });
  assert.equal(exit, 1, "no runId -> exit 1");
  assert.equal(relayTrack.skipNotify, true, "code leg must suppress its premature notification");
  assert.equal(finalizeCalls.length, 1, "finalizeCombo must fire exactly once");
  assert.equal(finalizeCalls[0].finalCode, 1, "finalize must carry the TRUE combo outcome");
  assert.equal(finalizeCalls[0].jobId, "job-1", "finalize must carry the relay's jobId");
});

test("implement exits 1 when code envelope has no runId", () => {
  const cwd = tempCwd();
  const codeOut = codeEnvelope({ runId: null, status: "success" });
  const callsPath = path.join(cwd, "calls.log");
  // Omit handoff registration: must not spawn handoff without a runId.
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeOut}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "implement",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "fix it",
      ],
      { cwd, env: companionEnv(env, cwd, callsPath) }
    );
    assert.equal(res.code, 1, `stderr: ${res.stderr}`);
    assert.ok(res.stdout.includes(codeOut) || res.stderr.includes("no runId"), res.stderr);
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode: 'handoff'"),
      "handoff must not be spawned when code has no runId"
    );
    assert.deepEqual(readCalls(callsPath), ["code"]);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

// Documented: handoff still runs after failed code WHEN a runId exists so
// not-ready blockers surface. Exit is 1 (not ready).
test("implement runs handoff after failed code with runId and exits 1", () => {
  const cwd = tempCwd();
  const codeOut = codeEnvelope({ status: "failure" });
  const handoffOut = handoffEnvelope(false);
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeOut}\n`, exitCode: 1 },
    handoff: { stdout: `${handoffOut}\n`, exitCode: 1 },
  });
  try {
    const res = runCompanion(
      [
        "implement",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "fix it",
      ],
      { cwd, env: companionEnv(env, cwd, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.ok(res.stdout.includes(codeOut), "code envelope must be relayed");
    assert.ok(res.stdout.includes(handoffOut), "handoff envelope must be relayed after failed code");
    assert.deepEqual(readCalls(callsPath), ["code", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

// no-runId path: any failure (including spawn failure) normalizes to exit 1
// rather than the raw spawn exit code.
test("implement no-runId spawn-failure path returns 1", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  // Unregistered code mode -> fake wrapper exits 2; implement must normalize to 1.
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(
      [
        "implement",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "fix it",
      ],
      { cwd, env: companionEnv(env, cwd, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1 (not raw spawn code); stderr: ${res.stderr}`);
    // Either code was never callable, or it ran and returned without a usable runId.
    const calls = readCalls(callsPath);
    assert.ok(
      calls.length === 0 || (calls.length === 1 && calls[0] === "code"),
      `unexpected calls: ${JSON.stringify(calls)}`
    );
    assert.ok(!calls.includes("handoff"), "handoff must not run without runId");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("implement refuses in direct run-mode", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  setRunMode(cwd, "direct", { CLAUDE_PLUGIN_DATA: pluginData });
  // Empty responses: any wrapper spawn fails the unregistered-mode probe (exit 2).
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(
      [
        "implement",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "fix it",
      ],
      {
        cwd,
        env: {
          ...env,
          CLAUDE_PLUGIN_DATA: pluginData,
          GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
        },
      }
    );
    assert.equal(res.code, 1, `stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.equal(res.code, 1);
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "direct refuse must happen before any wrapper spawn"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
