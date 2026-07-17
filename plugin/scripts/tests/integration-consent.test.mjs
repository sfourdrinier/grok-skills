// plugin/scripts/tests/integration-consent.test.mjs
//
// Task 7.2: one-time consent gate for --integration direct (companion).
// Orthogonal to runMode (hardened|direct security posture).

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  getIntegrationConsent,
  getIntegrationMode,
  getRunMode,
  setIntegrationMode,
  setRunMode,
} from "../lib/jobs.mjs";
import {
  companionIsolation,
  makeFakeWrapper,
  readCalls,
  runCompanion,
} from "./helpers/fake-wrapper.mjs";

const RID = "20260716T120000Z-abc123";

function codeEnvelope() {
  return JSON.stringify({
    schemaVersion: 1,
    mode: "code",
    status: "success",
    runId: RID,
    response: { text: "ok" },
  });
}

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-int-consent-"));
}

const TRUST_SUMMARY_RE =
  /Direct integration lets Grok edit files in THIS working tree directly/i;
const SETUP_CMD_RE = /setup --integration direct/;

test("RED: default code (no consent) refuses with trust summary; wrapper not spawned", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      { cwd, env: { ...env, FAKE_WRAPPER_CALLS: callsPath } }
    );
    assert.notEqual(res.code, 0, `expected non-zero refuse; got ${res.code}`);
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    assert.match(res.stderr, SETUP_CMD_RE);
    assert.match(res.stderr, /--integration worktree|--integration review/);
    // Wrapper must not have been spawned (no code call logged).
    assert.deepEqual(readCalls(callsPath), []);
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "refuse must happen before wrapper spawn (no unregistered-mode probe)"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("RED: explicit --integration direct without consent refuses", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      { cwd, env: { ...env, FAKE_WRAPPER_CALLS: callsPath } }
    );
    assert.notEqual(res.code, 0);
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    assert.match(res.stderr, SETUP_CMD_RE);
    assert.deepEqual(readCalls(callsPath), []);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("after setup --integration direct, code proceeds with explicit --integration direct", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };

  // setup --integration direct records mode + consent
  const setupRes = runCompanion(
    ["setup", "--integration", "direct", "--skip-codex-agents"],
    { cwd, env: envBase }
  );
  // setup may exit 1 without grok CLI; prefs must still apply
  assert.ok(setupRes.code === 0 || setupRes.code === 1, setupRes.stderr);
  assert.equal(getIntegrationMode(cwd, envBase), "direct");
  assert.equal(getIntegrationConsent(cwd, envBase), true);
  // runMode prefs untouched
  assert.equal(getRunMode(cwd, envBase), "hardened");

  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      {
        cwd,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsPath },
      }
    );
    assert.equal(res.code, 0, res.stderr);
    assert.deepEqual(readCalls(callsPath), ["code"]);
    const envelope = JSON.parse(res.stdout.trim());
    const argv = envelope.argv || [];
    const idx = argv.indexOf("--integration");
    assert.ok(idx >= 0, `expected --integration in argv: ${JSON.stringify(argv)}`);
    assert.equal(argv[idx + 1], "direct");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("code --integration worktree needs no consent; wrapper gets worktree", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      { cwd, env: { ...env, FAKE_WRAPPER_CALLS: callsPath } }
    );
    assert.equal(res.code, 0, res.stderr);
    assert.deepEqual(readCalls(callsPath), ["code"]);
    const envelope = JSON.parse(res.stdout.trim());
    const argv = envelope.argv || [];
    const idx = argv.indexOf("--integration");
    assert.ok(idx >= 0, `expected --integration in argv: ${JSON.stringify(argv)}`);
    assert.equal(argv[idx + 1], "worktree");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("env CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE=direct alone does not satisfy consent", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      {
        cwd,
        env: {
          ...env,
          FAKE_WRAPPER_CALLS: callsPath,
          CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "direct",
        },
      }
    );
    assert.notEqual(res.code, 0);
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    assert.deepEqual(readCalls(callsPath), []);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("integration prefs leave runMode hardened/direct untouched", () => {
  const cwd = tempCwd();
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(getRunMode(cwd, env), "hardened");
  setRunMode(cwd, "direct", env);
  assert.equal(getRunMode(cwd, env), "direct");

  setIntegrationMode(cwd, "worktree", env);
  assert.equal(getIntegrationMode(cwd, env), "worktree");
  assert.equal(getIntegrationConsent(cwd, env), false);
  assert.equal(getRunMode(cwd, env), "direct", "runMode must stay direct");

  setIntegrationMode(cwd, "direct", env);
  assert.equal(getIntegrationMode(cwd, env), "direct");
  assert.equal(getIntegrationConsent(cwd, env), true);
  assert.equal(getRunMode(cwd, env), "direct", "runMode must stay direct after consent");

  setRunMode(cwd, "hardened", env);
  assert.equal(getRunMode(cwd, env), "hardened");
  assert.equal(getIntegrationMode(cwd, env), "direct");
  assert.equal(getIntegrationConsent(cwd, env), true);
  fs.rmSync(cwd, { recursive: true, force: true });
});

test("getIntegrationMode precedence: setup > CLAUDE_PLUGIN_OPTION_* > default", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");

  assert.equal(
    getIntegrationMode(cwd, { CLAUDE_PLUGIN_DATA: pluginData }),
    "direct"
  );

  assert.equal(
    getIntegrationMode(cwd, {
      CLAUDE_PLUGIN_DATA: pluginData,
      CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "worktree",
    }),
    "worktree"
  );

  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata2"),
    CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "worktree",
  };
  setIntegrationMode(cwd, "review", env);
  assert.equal(getIntegrationMode(cwd, env), "review");
  // Consent only for direct
  assert.equal(getIntegrationConsent(cwd, env), false);
  fs.rmSync(cwd, { recursive: true, force: true });
});

test("companionIsolation fresh workspace has no consent", () => {
  const iso = companionIsolation({});
  try {
    assert.equal(getIntegrationConsent(iso.cwd, iso.env), false);
    assert.equal(getIntegrationMode(iso.cwd, iso.env), "direct");
  } finally {
    iso.cleanup();
  }
});
