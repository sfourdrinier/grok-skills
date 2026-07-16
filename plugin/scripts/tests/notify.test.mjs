import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  attemptNotify,
  getExecutionContext,
  shouldNotify,
  wrapperChildEnv,
  NOTIFY_ELIGIBLE_MODES,
} from "../lib/notify.mjs";
import {
  DEFAULT_JOBS_CONFIG,
  getNotificationConfig,
  setNotificationConfig,
  setRunMode,
  getRunMode,
} from "../lib/jobs.mjs";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-notify-"));
}

test("getExecutionContext defaults to foreground", () => {
  assert.equal(getExecutionContext({}), "foreground");
  assert.equal(getExecutionContext({ GROK_COMPANION_EXECUTION_CONTEXT: "nope" }), "foreground");
  assert.equal(getExecutionContext({ GROK_COMPANION_EXECUTION_CONTEXT: "background" }), "background");
});

test("shouldNotify matrix for modes", () => {
  assert.equal(shouldNotify({ notificationMode: "off", executionContext: "background" }).notify, false);
  assert.equal(shouldNotify({ notificationMode: "auto", executionContext: "foreground" }).notify, false);
  assert.equal(shouldNotify({ notificationMode: "auto", executionContext: "background" }).notify, true);
  assert.equal(shouldNotify({ notificationMode: "native", executionContext: "foreground" }).notify, true);
  assert.equal(
    shouldNotify({ notificationMode: "webhook", executionContext: "background", webhookUrl: null }).notify,
    false
  );
  assert.equal(
    shouldNotify({
      notificationMode: "webhook",
      executionContext: "foreground",
      webhookUrl: "https://example.com/hook",
    }).notify,
    true
  );
});

test("wrapperChildEnv strips execution context", () => {
  const env = wrapperChildEnv({
    GROK_COMPANION_EXECUTION_CONTEXT: "background",
    PATH: "/usr/bin",
  });
  assert.equal(env.GROK_COMPANION_EXECUTION_CONTEXT, undefined);
  assert.equal(env.PATH, "/usr/bin");
});

test("jobs notification defaults are off / null", () => {
  const cwd = tempCwd();
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const cfg = getNotificationConfig(cwd, env);
  assert.equal(cfg.notificationMode, DEFAULT_JOBS_CONFIG.notificationMode);
  assert.equal(cfg.notificationWebhookUrl, null);
  assert.equal(DEFAULT_JOBS_CONFIG.notificationMode, "off");
});

test("setNotificationConfig persists mode and webhook", () => {
  const cwd = tempCwd();
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  setRunMode(cwd, "hardened", env);
  const saved = setNotificationConfig(
    cwd,
    { notificationMode: "auto", notificationWebhookUrl: "https://example.com/h" },
    env
  );
  assert.equal(saved.notificationMode, "auto");
  assert.equal(saved.notificationWebhookUrl, "https://example.com/h");
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "auto");
  assert.equal(getRunMode(cwd, env), "hardened");
});

test("attemptNotify off is a no-op without marker", async () => {
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const result = await attemptNotify({
    runDir,
    runId: "20260716T000000Z-abcdef",
    mode: "review",
    lifecycle: "completed",
    notificationMode: "off",
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "background" },
  });
  assert.equal(result.attempted, false);
  assert.equal(result.reason, "mode-off");
  assert.equal(fs.existsSync(path.join(runDir, "notified.json")), false);
});

test("attemptNotify already-attempted after first call", async () => {
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const runId = "20260716T000001Z-abcdef";
  const env = { GROK_COMPANION_EXECUTION_CONTEXT: "background" };
  const first = await attemptNotify({
    runDir,
    runId,
    mode: "review",
    lifecycle: "completed",
    notificationMode: "native",
    env,
  });
  assert.equal(first.attempted, true);
  assert.ok(fs.existsSync(path.join(runDir, "notified.json")));
  const second = await attemptNotify({
    runDir,
    runId,
    mode: "review",
    lifecycle: "completed",
    notificationMode: "native",
    env,
  });
  assert.equal(second.attempted, false);
  assert.equal(second.reason, "already-attempted");
  const marker = JSON.parse(fs.readFileSync(path.join(runDir, "notified.json"), "utf8"));
  assert.equal(marker.state, "completed");
});

test("pending marker blocks auto re-attempt (crash-left pending)", async () => {
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const runId = "20260716T000002Z-abcdef";
  fs.writeFileSync(
    path.join(runDir, "notified.json"),
    JSON.stringify({ state: "pending", attemptedAt: new Date().toISOString(), adapter: null, result: null }),
    "utf8"
  );
  const result = await attemptNotify({
    runDir,
    runId,
    mode: "review",
    lifecycle: "completed",
    notificationMode: "native",
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "background" },
  });
  assert.equal(result.attempted, false);
  assert.equal(result.reason, "already-attempted");
});

test("webhook mode POSTs and records completed", async () => {
  let sawBody = null;
  const server = http.createServer((req, res) => {
    let data = "";
    req.on("data", (c) => {
      data += c;
    });
    req.on("end", () => {
      sawBody = JSON.parse(data);
      res.writeHead(204);
      res.end();
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const { port } = server.address();
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const runId = "20260716T000003Z-abcdef";
  const result = await attemptNotify({
    runDir,
    runId,
    mode: "code",
    lifecycle: "completed",
    durationSeconds: 12,
    notificationMode: "webhook",
    webhookUrl: `http://127.0.0.1:${port}/hook`,
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "foreground" },
  });
  server.close();
  assert.equal(result.attempted, true);
  assert.equal(result.sent, true);
  assert.equal(sawBody.runId, runId);
  assert.equal(sawBody.mode, "code");
  assert.equal(sawBody.lifecycle, "completed");
  assert.equal(sawBody.durationSeconds, 12);
  const marker = JSON.parse(fs.readFileSync(path.join(runDir, "notified.json"), "utf8"));
  assert.equal(marker.state, "completed");
  assert.equal(marker.result, "sent");
  assert.equal(marker.adapter, "webhook");
});

test("status mode is not notify-eligible", () => {
  assert.equal(NOTIFY_ELIGIBLE_MODES.has("status"), false);
  assert.equal(NOTIFY_ELIGIBLE_MODES.has("setup"), false);
  assert.equal(NOTIFY_ELIGIBLE_MODES.has("review"), true);
});
