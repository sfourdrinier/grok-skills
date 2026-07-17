// plugin/scripts/tests/fake-wrapper.test.mjs
//
// Contract tests for the canonical fake-wrapper harness (tests/helpers/
// fake-wrapper.mjs): companion tests never spawn the real wrapper or the Grok
// CLI; they register per-mode canned responses instead.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { makeFakeWrapper, readCalls, runCompanion } from "./helpers/fake-wrapper.mjs";

const RID = "20260716T000000Z-abc123";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-fakewrap-"));
}

test("fake wrapper answers per-mode and companion relays it", () => {
  const envelope = JSON.stringify({ status: "success", runId: RID, mode: "status" });
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: envelope, exitCode: 0 },
  });
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.equal(res.code, 0);
    assert.ok(res.stdout.includes(envelope), `stdout missing envelope: ${res.stdout}`);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("fake wrapper nonzero exit propagates", () => {
  const { env, cleanup } = makeFakeWrapper({ status: { stdout: "{}", exitCode: 1 } });
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.equal(res.code, 1);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("unregistered mode exits 2 (the handoff-not-spawned probe)", () => {
  const { env, cleanup } = makeFakeWrapper({});
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.equal(res.code, 2);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("fake wrapper stderr is relayed", () => {
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: "{}", exitCode: 0, stderr: "[fake] diagnostic line\n" },
  });
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.match(res.stderr, /\[fake\] diagnostic line/);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("FAKE_WRAPPER_CALLS appends invoked mode; readCalls returns string[]", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: "{}", exitCode: 0 },
  });
  try {
    assert.deepEqual(readCalls(callsPath), []);
    const res = runCompanion(["status", "--run-id", RID], {
      env: { ...env, FAKE_WRAPPER_CALLS: callsPath },
      cwd,
    });
    assert.equal(res.code, 0);
    assert.deepEqual(readCalls(callsPath), ["status"]);
    // Second call appends
    runCompanion(["status", "--run-id", RID], {
      env: { ...env, FAKE_WRAPPER_CALLS: callsPath },
      cwd,
    });
    assert.deepEqual(readCalls(callsPath), ["status", "status"]);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
