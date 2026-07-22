// plugin/scripts/tests/setup-json.test.mjs
//
// Issue #8 / adversarial review: setup --json must reach cmdSetup after
// stripFlags peels companion-only flags, and must not dump raw webhook URLs.
// CI has no real `grok` on PATH - provide GROK_AGENT_BINARY fake.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { setNotificationConfig } from "../lib/jobs.mjs";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

const RID = "20260722T000000Z-setup1";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-setup-json-"));
}

/** Minimal `grok --version` binary for setup readiness (CI has no real grok). */
function installFakeGrok() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-bin-"));
  const binary = path.join(dir, "grok");
  fs.writeFileSync(binary, "#!/bin/sh\necho 'grok 0.0.0-test'\n", { mode: 0o755 });
  return {
    binary,
    cleanup: () => fs.rmSync(dir, { recursive: true, force: true }),
  };
}

function setupEnv(fakeEnv, cwd) {
  const { binary, cleanup: binCleanup } = installFakeGrok();
  return {
    env: {
      ...fakeEnv,
      GROK_AGENT_BINARY: binary,
      CLAUDE_PLUGIN_DATA: path.join(cwd, ".grok-plugin-data"),
    },
    cleanupBin: binCleanup,
  };
}

test("setup --json emits machine-readable status on stdout", () => {
  const cwd = tempCwd();
  const { env: fakeEnv, cleanup } = makeFakeWrapper({
    preflight: {
      stdout:
        JSON.stringify({
          schemaVersion: 1,
          mode: "preflight",
          status: "success",
          runId: RID,
          response: { checks: [{ name: "grokVersion", ok: true, detail: "grok 0.0.0" }] },
        }) + "\n",
      exitCode: 0,
    },
  });
  const { env, cleanupBin } = setupEnv(fakeEnv, cwd);
  try {
    const res = runCompanion(["setup", "--json", "--run-mode", "hardened"], { cwd, env });
    assert.equal(res.code, 0, res.stderr || res.stdout);
    const line = String(res.stdout)
      .trim()
      .split("\n")
      .filter(Boolean)
      .pop();
    const body = JSON.parse(line);
    assert.equal(body.mode, "setup");
    assert.equal(body.schemaVersion, 1);
    assert.ok(["success", "failure"].includes(body.status));
    assert.equal(body.runMode, "hardened");
    assert.ok(Array.isArray(body.checks));
    assert.ok(body.notifications);
  } finally {
    cleanupBin();
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("setup --json redacts webhook URL path secrets", () => {
  const cwd = tempCwd();
  const secretPath = "https://hooks.example.com/services/T00/B00/SECRETTOKEN";
  const { env: fakeEnv, cleanup } = makeFakeWrapper({
    preflight: {
      stdout:
        JSON.stringify({
          schemaVersion: 1,
          mode: "preflight",
          status: "success",
          runId: RID,
          response: { checks: [] },
        }) + "\n",
      exitCode: 0,
    },
  });
  const { env, cleanupBin } = setupEnv(fakeEnv, cwd);
  try {
    setNotificationConfig(
      cwd,
      { notificationMode: "webhook", notificationWebhookUrl: secretPath },
      env
    );
    const res = runCompanion(["setup", "--json"], { cwd, env });
    assert.equal(res.code, 0, res.stderr || res.stdout);
    const body = JSON.parse(
      String(res.stdout)
        .trim()
        .split("\n")
        .filter(Boolean)
        .pop()
    );
    assert.equal(body.notifications.webhookConfigured, true);
    assert.ok(body.notifications.webhookUrl);
    assert.ok(!String(body.notifications.webhookUrl).includes("SECRETTOKEN"));
    assert.ok(!String(res.stdout).includes("SECRETTOKEN"));
  } finally {
    cleanupBin();
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
