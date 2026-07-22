// plugin/scripts/tests/integration-consent.test.mjs
//
// 2.0.1+: integration consent gates are removed. Direct lands without setup.
// This file keeps coverage that default direct works and cross-repo prefs still
// key on target workspace (mode prefs only — no consent refuse).

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
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-int-direct-"));
}

test("default code (no setup) lands direct — wrapper is spawned", () => {
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
    assert.equal(res.code, 0, res.stderr || res.stdout);
    const calls = readCalls(callsPath);
    assert.ok(calls.length >= 1, "wrapper must spawn without consent gate");
    assert.ok(calls.includes("code"), `expected code mode spawn, got ${calls}`);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("explicit --integration direct works without setup", () => {
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
    assert.equal(res.code, 0, res.stderr || res.stdout);
    assert.ok(readCalls(callsPath).includes("code"));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("code --integration worktree still spawns wrapper", () => {
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
    assert.equal(res.code, 0, res.stderr || res.stdout);
    assert.ok(readCalls(callsPath).includes("code"));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("getIntegrationConsent always true (legacy no-op)", () => {
  const cwd = tempCwd();
  try {
    assert.equal(getIntegrationConsent(cwd, {}), true);
    setIntegrationMode(cwd, "worktree", {});
    assert.equal(getIntegrationConsent(cwd, {}), true);
    setIntegrationMode(cwd, "direct", {});
    assert.equal(getIntegrationConsent(cwd, {}), true);
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("setup --integration direct sets mode without a consent gate", () => {
  const cwd = tempCwd();
  const { env, cleanup } = makeFakeWrapper({
    preflight: {
      stdout: JSON.stringify({
        schemaVersion: 1,
        mode: "preflight",
        status: "success",
        runId: RID,
        response: { checks: [] },
      }) + "\n",
      exitCode: 0,
    },
  });
  try {
    setRunMode(cwd, "direct", env);
    const res = runCompanion(["setup", "--integration", "direct"], { cwd, env });
    assert.equal(res.code, 0, res.stderr || res.stdout);
    assert.equal(getIntegrationMode(cwd, env), "direct");
    assert.equal(getRunMode(cwd, env), "direct");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("companionIsolation workspace allows direct immediately", () => {
  const iso = companionIsolation();
  try {
    assert.equal(getIntegrationConsent(iso.cwd, iso.env), true);
  } finally {
    iso.cleanup();
  }
});
