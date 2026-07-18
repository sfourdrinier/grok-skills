// plugin/scripts/tests/direct-parity.test.mjs
//
// Task 1.6: direct-mode job-surface parity + honest handoff/status refusal.
// Fake-wrapper only - unregistered mode exit 2 proves no wrapper spawn.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  DIRECT_NO_HANDOFF_MSG,
  DIRECT_RUN_ID_RE,
  runDirectGrok,
} from "../lib/direct-grok.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
import {
  createJob,
  storeJobStdout,
  updateJob,
} from "../lib/jobs.mjs";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

const DIRECT_ID = "direct-1234567890";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-parity-"));
}

test("[4] runDirectGrok redacts secrets in the direct-mode envelope", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-redact-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    const secret = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nprintf '%s\\n' '{"result":"here is a token ${secret} end"}'\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(secret),
      `secret must be redacted from the direct envelope: ${res.envelopeText}`
    );
    assert.match(res.envelopeText, /redacted/i, "redaction marker expected");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("result resolves direct-<timestamp> runId via the job index", () => {
  assert.ok(DIRECT_RUN_ID_RE.test(DIRECT_ID));
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  const job = createJob(cwd, { kind: "review", mode: "review", runMode: "direct" }, envBase);
  updateJob(cwd, job.id, { runId: DIRECT_ID, status: "success" }, envBase);
  const payload = JSON.stringify({
    status: "success",
    mode: "review",
    runId: DIRECT_ID,
    response: { text: "direct-job-output" },
  });
  storeJobStdout(cwd, job.id, `${payload}\n`, envBase);

  // Empty fake wrapper: result must not need the wrapper at all.
  const { env: fakeEnv, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["result", DIRECT_ID], {
      cwd,
      env: { ...fakeEnv, CLAUDE_PLUGIN_DATA: pluginData },
    });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.match(res.stdout, /direct-job-output/);
    assert.match(res.stdout, new RegExp(DIRECT_ID));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("handoff --run-id direct-* refuses before wrapper spawn", () => {
  const cwd = tempCwd();
  // Empty responses: if wrapper were spawned, unregistered mode exits 2.
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["handoff", "--run-id", DIRECT_ID], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") },
    });
    assert.equal(res.code, 1, `expected exit 1; got ${res.code}; stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "handoff direct-id refuse must not spawn the wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("status --run-id direct-* refuses with the same shared message", () => {
  const cwd = tempCwd();
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["status", "--run-id", DIRECT_ID], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") },
    });
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "status direct-id refuse must not spawn the wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("DIRECT_NO_HANDOFF_MSG is the single shared refusal string", () => {
  assert.equal(typeof DIRECT_NO_HANDOFF_MSG, "string");
  assert.match(DIRECT_NO_HANDOFF_MSG, /direct-mode runs have no hardened run state/);
  assert.match(DIRECT_NO_HANDOFF_MSG, /setup --run-mode hardened/);
});
