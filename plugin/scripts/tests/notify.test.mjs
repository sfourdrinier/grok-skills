import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
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
  NOTIFICATION_MODES,
} from "../lib/jobs.mjs";
import { RUN_ID_RE } from "../progress-relay.mjs";

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

test("attemptNotify refuses missing run dir without creating it", async () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-parent-"));
  const runDir = path.join(parent, "no-such-run");
  const result = await attemptNotify({
    runDir,
    runId: "20260716T000099Z-abcdef",
    mode: "review",
    lifecycle: "completed",
    notificationMode: "native",
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "background" },
  });
  assert.equal(result.attempted, false);
  assert.equal(result.reason, "run-dir-missing");
  assert.equal(fs.existsSync(runDir), false);
});

test("auto mode skips in foreground without writing marker", async () => {
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const result = await attemptNotify({
    runDir,
    runId: "20260716T000100Z-abcdef",
    mode: "review",
    lifecycle: "completed",
    notificationMode: "auto",
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "foreground" },
  });
  assert.equal(result.attempted, false);
  assert.equal(result.reason, "auto-foreground");
  assert.equal(fs.existsSync(path.join(runDir, "notified.json")), false);
});

test("ineligible mode never writes marker", async () => {
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const result = await attemptNotify({
    runDir,
    runId: "20260716T000101Z-abcdef",
    mode: "status",
    lifecycle: "completed",
    notificationMode: "native",
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "background" },
  });
  assert.equal(result.attempted, false);
  assert.equal(result.reason, "mode-not-eligible");
  assert.equal(fs.existsSync(path.join(runDir, "notified.json")), false);
});

test("webhook non-2xx still completes marker as failed (no auto-retry)", async () => {
  const server = http.createServer((_req, res) => {
    res.writeHead(500);
    res.end("nope");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const { port } = server.address();
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-run-"));
  const runId = "20260716T000102Z-abcdef";
  const first = await attemptNotify({
    runDir,
    runId,
    mode: "review",
    lifecycle: "failed",
    notificationMode: "webhook",
    webhookUrl: `http://127.0.0.1:${port}/hook`,
    env: {},
  });
  server.close();
  assert.equal(first.attempted, true);
  assert.equal(first.sent, false);
  const marker = JSON.parse(fs.readFileSync(path.join(runDir, "notified.json"), "utf8"));
  assert.equal(marker.state, "completed");
  assert.equal(marker.result, "failed");
  const second = await attemptNotify({
    runDir,
    runId,
    mode: "review",
    lifecycle: "failed",
    notificationMode: "webhook",
    webhookUrl: `http://127.0.0.1:${port}/hook`,
    env: {},
  });
  assert.equal(second.reason, "already-attempted");
});

test("wrapperChildEnv is pure and does not mutate input", () => {
  const base = { GROK_COMPANION_EXECUTION_CONTEXT: "background", FOO: "1" };
  const out = wrapperChildEnv(base);
  assert.equal(out.GROK_COMPANION_EXECUTION_CONTEXT, undefined);
  assert.equal(base.GROK_COMPANION_EXECUTION_CONTEXT, "background");
  assert.equal(out.FOO, "1");
});

test("notification mode set is complete and stable", () => {
  assert.deepEqual([...NOTIFICATION_MODES].sort(), ["auto", "native", "off", "webhook"]);
});

test("RUN_ID_RE rejects path-traversal shaped ids", () => {
  assert.equal(RUN_ID_RE.test("20260716T000000Z-abcdef"), true);
  assert.equal(RUN_ID_RE.test("../evil"), false);
  assert.equal(RUN_ID_RE.test("20260716T000000Z-abcdef/../x"), false);
  assert.equal(RUN_ID_RE.test("direct-123"), false);
});

test("skills declare execution context prefix (contract)", () => {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const files = [
    "skills/review/SKILL.md",
    "skills/code/SKILL.md",
    "skills/reason/SKILL.md",
    "skills/verify/SKILL.md",
    "skills/adversarial-review/SKILL.md",
    "agents/grok-engineer-coder.md",
    "agents/grok-rescue.md",
    "references/execution-context.md",
  ];
  for (const rel of files) {
    const text = fs.readFileSync(path.join(root, rel), "utf8");
    assert.match(
      text,
      /GROK_COMPANION_EXECUTION_CONTEXT/,
      `${rel} must document GROK_COMPANION_EXECUTION_CONTEXT`
    );
  }
});

test("setNotificationConfig rejects invalid mode (keeps default)", () => {
  const cwd = tempCwd();
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  setRunMode(cwd, "hardened", env);
  setNotificationConfig(cwd, { notificationMode: "telepathy" }, env);
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "off");
  setNotificationConfig(cwd, { notificationMode: "auto" }, env);
  setNotificationConfig(cwd, { notificationMode: "BOGUS" }, env);
  // invalid normalize falls back to default off, not previous auto
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "off");
});

test("adversarial-review is notify-eligible and webhook body uses skill mode", async () => {
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
  const runId = "20260716T000200Z-abcdef";
  const result = await attemptNotify({
    runDir,
    runId,
    mode: "adversarial-review",
    lifecycle: "completed",
    durationSeconds: 3,
    notificationMode: "webhook",
    webhookUrl: `http://127.0.0.1:${port}/hook`,
    env: { GROK_COMPANION_EXECUTION_CONTEXT: "background" },
  });
  server.close();
  assert.equal(result.attempted, true);
  assert.equal(result.sent, true);
  assert.equal(sawBody.mode, "adversarial-review");
  assert.equal(sawBody.lifecycle, "completed");
  assert.notEqual(sawBody.lifecycle, "running");
});

test("notify body is ASCII separators only (no middle-dot)", async () => {
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
  // Force native path via webhook for body inspect of payload fields only;
  // webhook JSON is the durable body contract for operators.
  await attemptNotify({
    runDir,
    runId: "20260716T000201Z-abcdef",
    mode: "review",
    lifecycle: "failed",
    durationSeconds: 9,
    notificationMode: "webhook",
    webhookUrl: `http://127.0.0.1:${port}/h`,
    env: {},
  });
  server.close();
  const serialized = JSON.stringify(sawBody);
  assert.equal(serialized.includes("\u00b7"), false);
  assert.equal(sawBody.lifecycle, "failed");
});
