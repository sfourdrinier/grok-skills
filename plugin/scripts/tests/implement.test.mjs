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
import {
  buildAutoCodeFallbackEnvelope,
  runAutoIntegrate,
  runImplementCombo,
  runHandoffCaptured,
} from "../lib/implement.mjs";
import { listJobs, readJobStdout } from "../lib/jobs.mjs";
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

// Auto's apply-time revalidation reuses runHandoffCaptured only to re-check
// readiness; it must NOT relay a second stdout envelope after the initial
// handoff already emitted (one-stdout-envelope contract).
test("runHandoffCaptured silent: parses envelope but does not relay stdout", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-handoff-silent-"));
  const wrapper = path.join(dir, "grok_agent.py");
  const envJson = handoffEnvelope(true);
  fs.writeFileSync(
    wrapper,
    `import sys\nsys.stdout.write(${JSON.stringify(envJson)} + "\\n")\nsys.exit(0)\n`,
    { mode: 0o600 }
  );
  const writes = [];
  const orig = process.stdout.write.bind(process.stdout);
  process.stdout.write = (s, enc, cb) => {
    writes.push(String(s));
    if (typeof enc === "function") enc();
    else if (typeof cb === "function") cb();
    return true;
  };
  let silent;
  let afterSilent;
  let loud;
  try {
    silent = runHandoffCaptured(wrapper, ["handoff", "--run-id", RUN_ID], { silent: true });
    afterSilent = writes.length;
    loud = runHandoffCaptured(wrapper, ["handoff", "--run-id", RUN_ID]);
  } finally {
    process.stdout.write = orig;
    fs.rmSync(dir, { recursive: true, force: true });
  }
  // Silent: envelope captured, NOTHING written to stdout.
  assert.equal(silent.code, 0);
  assert.equal(silent.envelope?.response?.integration?.ready, true);
  assert.equal(afterSilent, 0, "silent capture must not relay to stdout");
  // Default (non-silent): still relays, proving the option is what gates it.
  assert.equal(loud.code, 0);
  assert.ok(writes.join("").includes('"handoff"'), "default must relay stdout");
});

// worktree/review integrations need the hardened wrapper's isolated worktree; a
// direct-mode workspace pref must NOT reroute them to runDirectGrok's live edit.
test("code --integration worktree in a direct workspace uses the wrapper, not runDirectGrok", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  setRunMode(cwd, "direct", { CLAUDE_PLUGIN_DATA: pluginData });
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      ["code", "--integration", "worktree", "--target", ".", "--base", "HEAD", "--task", "x"],
      { cwd, env: companionEnv(env, cwd, callsPath) }
    );
    // The hardened fake wrapper WAS invoked -> worktree isolation honored via the
    // wrapper, not silently downgraded to a live runDirectGrok edit.
    assert.deepEqual(readCalls(callsPath), ["code"], `stderr: ${res.stderr}`);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

// continue-run is worktree-only in the wrapper (retained lineage); a direct-mode
// workspace pref must NOT reroute it to runDirectGrok's live-tree edit.
test("continue-run in a direct workspace uses the wrapper, never runDirectGrok", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  setRunMode(cwd, "direct", { CLAUDE_PLUGIN_DATA: pluginData });
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(["code", "--continue-run", RUN_ID], {
      cwd,
      env: companionEnv(env, cwd, callsPath),
    });
    // The hardened fake wrapper WAS invoked -> continuation took the wrapper path,
    // not runDirectGrok (which would never call the fake wrapper).
    assert.deepEqual(readCalls(callsPath), ["code"], `stderr: ${res.stderr}`);
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

// PR review #discussion_r3609529639: when code --integration auto has no
// parseable code envelope, emit a COMPLETE failure envelope (schemaVersion,
// mode=code, status=failure, runId policy, classified error, integration
// applied=false/outcome) - never a partial schemaVersion/mode/status-only object.
test("unit: empty codeEnvelope synthesizes a complete auto failure envelope", () => {
  const env = buildAutoCodeFallbackEnvelope({
    codeEnvelope: null,
    runId: null,
    jobId: "job-empty-code",
    codeExit: 4,
    codeStdout: "",
  });
  assert.equal(env.schemaVersion, 1);
  assert.equal(env.mode, "code");
  assert.equal(env.status, "failure");
  assert.equal(env.runId, null, "no known run id -> explicit null (not invented)");
  assert.equal(typeof env.error, "object");
  assert.equal(typeof env.error.class, "string");
  assert.ok(env.error.class.length > 0, "classified error class required");
  assert.equal(typeof env.error.message, "string");
  assert.ok(env.error.message.length > 0, "error message must surface the spawn/parse failure");
  assert.equal(env.response?.integration?.applied, false);
  assert.equal(env.response?.integration?.ready, false);
  assert.equal(typeof env.response?.integration?.outcome, "string");
  assert.ok(env.response.integration.outcome.length > 0);
});

test("unit: unparseable code stdout still synthesizes complete failure (preserves known runId)", () => {
  const env = buildAutoCodeFallbackEnvelope({
    codeEnvelope: null,
    runId: RUN_ID,
    jobId: null,
    codeExit: 1,
    codeStdout: "not-json-at-all\n",
  });
  assert.equal(env.schemaVersion, 1);
  assert.equal(env.mode, "code");
  assert.equal(env.status, "failure");
  assert.equal(env.runId, RUN_ID, "known result/job run id must be preserved");
  assert.equal(typeof env.error?.class, "string");
  assert.ok(env.error.class.length > 0);
  assert.equal(env.response?.integration?.applied, false);
  assert.notEqual(env.status, "success");
});

// Parseable code envelope without a usable runId is not stdout corruption:
// classify handoff-unavailable (cannot hand off / apply), never output-malformed.
test("unit: parseable code envelope missing runId is handoff-unavailable (not output-malformed)", () => {
  const codeOut = {
    schemaVersion: 1,
    mode: "code",
    status: "success",
    runId: null,
    response: { text: "ok", integration: { ready: true } },
  };
  const env = buildAutoCodeFallbackEnvelope({
    codeEnvelope: codeOut,
    runId: null,
    jobId: "job-no-runid",
    codeExit: 0,
    // stdout is the JSON envelope itself - parseable, so NOT output-malformed
    codeStdout: `${JSON.stringify(codeOut)}\n`,
  });
  assert.equal(env.schemaVersion, 1);
  assert.equal(env.mode, "code");
  assert.equal(env.status, "failure");
  assert.equal(env.runId, null, "must not invent a runId");
  assert.equal(
    env.error?.class,
    "handoff-unavailable",
    "parseable envelope without runId is handoff-unavailable, not output-malformed"
  );
  assert.notEqual(env.error?.class, "output-malformed");
  assert.notEqual(env.error?.class, "output-missing");
  assert.equal(env.response?.integration?.applied, false);
  assert.equal(env.response?.integration?.ready, false);
  assert.equal(env.response?.integration?.outcome, "not-ready");
});

test("auto: empty code envelope yields one complete failure envelope on stdout + store", async () => {
  const finalizeCalls = [];
  const writes = [];
  const origWrite = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, enc, cb) => {
    writes.push(String(chunk));
    if (typeof enc === "function") enc();
    else if (typeof cb === "function") cb();
    return true;
  };
  let code;
  try {
    code = await runAutoIntegrate(
      "wrapper",
      ["--integration", "auto", "--target", ".", "--base", "HEAD", "--task", "x"],
      "hardened",
      { kind: "code", mode: "code", notifyMode: "code", runMode: "hardened" },
      {
        runWithLiveRelay: async () => ({
          code: 4,
          stdout: "",
          jobId: "job-empty-auto",
        }),
        stderrLine: () => {},
        finalizeCombo: async (x) => finalizeCalls.push(x),
      }
    );
  } finally {
    process.stdout.write = origWrite;
  }
  assert.equal(code, 1);
  const stdout = writes.join("");
  const envLines = stdout.split("\n").filter((l) => l.trim().startsWith("{"));
  assert.equal(envLines.length, 1, `one envelope; got: ${stdout}`);
  const finalEnv = JSON.parse(envLines[0]);
  assert.equal(finalEnv.schemaVersion, 1);
  assert.equal(finalEnv.mode, "code");
  assert.equal(finalEnv.status, "failure");
  assert.equal(finalEnv.runId, null);
  assert.equal(typeof finalEnv.error?.class, "string");
  assert.ok(finalEnv.error.class.length > 0);
  assert.equal(typeof finalEnv.error?.message, "string");
  assert.ok(finalEnv.error.message.length > 0);
  assert.equal(finalEnv.response?.integration?.applied, false);
  assert.equal(finalEnv.response?.integration?.ready, false);
  assert.equal(typeof finalEnv.response?.integration?.outcome, "string");
  // Stored result must be the same complete failure envelope (safe for /grok:result).
  assert.equal(finalizeCalls.length, 1);
  assert.equal(finalizeCalls[0].finalCode, 1);
  const stored = JSON.parse(String(finalizeCalls[0].finalEnvelopeText).trim());
  assert.equal(stored.status, "failure");
  assert.equal(typeof stored.error?.class, "string");
  assert.equal(stored.response?.integration?.applied, false);
  assert.notEqual(stored.status, "success");
});


test("implement missing handoff envelope stores fallback failure (not code-leg success)", () => {
  // Handoff returns non-JSON (null parse) after a successful code leg with runId.
  // Stored finalEnvelopeText must be the fallback SSOT failure envelope, never the
  // code-leg success payload, so /grok:result cannot claim success without handoff.
  const cwd = tempCwd();
  const codeOut = codeEnvelope();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeOut}\n`, exitCode: 0 },
    // Non-JSON handoff stdout -> tryParseEnvelope null
    handoff: { stdout: "not-json-handoff\n", exitCode: 0 },
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
    const pluginData = path.join(cwd, "pdata");
    const jobs = listJobs(cwd, { CLAUDE_PLUGIN_DATA: pluginData });
    assert.ok(jobs.length >= 1, "implement must create a job");
    const job = jobs[0];
    assert.equal(job.status, "failure", "job status must reflect missing handoff");
    const stored = readJobStdout(cwd, job.id, { CLAUDE_PLUGIN_DATA: pluginData });
    assert.ok(stored, "job stdout must be stored");
    const envStored = JSON.parse(String(stored).trim().split("\n").filter(Boolean).pop());
    assert.equal(envStored.status, "failure", "stored must NOT be code-leg success");
    assert.notEqual(envStored.status, "success");
    assert.equal(envStored.mode, "code");
    assert.equal(envStored.response?.integration?.applied, false);
    assert.equal(typeof envStored.error?.class, "string");
    assert.ok(envStored.error.class.length > 0);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
