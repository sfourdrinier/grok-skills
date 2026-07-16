import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  createJob,
  DEFAULT_JOBS_CONFIG,
  formatJobsTable,
  getJob,
  getNotificationConfig,
  getRunMode,
  isNotificationMode,
  listJobs,
  NOTIFICATION_MODES,
  setNotificationConfig,
  setRunMode,
  storeJobStdout,
  updateJob,
} from "../lib/jobs.mjs";
import { runDirectGrok } from "../lib/direct-grok.mjs";
import { renderEnvelopePretty, tryParseEnvelope } from "../lib/render.mjs";
import { buildAdversarialTask } from "../lib/git-context.mjs";

test("job registry creates, lists, and updates", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-jobs-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(cwd, { kind: "review", mode: "review", runMode: "hardened" }, env);
  assert.ok(job.id);
  storeJobStdout(cwd, job.id, '{"status":"success","mode":"review"}\n', env);
  updateJob(cwd, job.id, { status: "success", summary: "ok" }, env);
  const listed = listJobs(cwd, env);
  assert.equal(listed[0].id, job.id);
  assert.equal(getJob(cwd, job.id, env).status, "success");
  const table = formatJobsTable(listed);
  assert.match(table, /review/);
});

test("run mode persists hardened vs direct", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-mode-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(getRunMode(cwd, env), "hardened");
  assert.equal(setRunMode(cwd, "direct", env), "direct");
  assert.equal(getRunMode(cwd, env), "direct");
  assert.equal(setRunMode(cwd, "hardened", env), "hardened");
});

test("notification prefs default off and persist", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-notify-cfg-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "off");
  assert.equal(DEFAULT_JOBS_CONFIG.notificationMode, "off");
  setNotificationConfig(cwd, { notificationMode: "auto" }, env);
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "auto");
});

test("NOTIFICATION_MODES re-exports the shared product set", () => {
  assert.deepEqual([...NOTIFICATION_MODES], ["off", "auto", "native", "webhook"]);
  assert.equal(isNotificationMode("auto"), true);
  assert.equal(isNotificationMode("telepathy"), false);
});

test("createJob records skill mode (e.g. adversarial-review)", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-jobs-skill-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(
    cwd,
    { kind: "adversarial-review", mode: "adversarial-review", runMode: "hardened" },
    env
  );
  assert.equal(getJob(cwd, job.id, env).mode, "adversarial-review");
});

test("direct mode rejects --isolated fail-closed", () => {
  const result = runDirectGrok({
    mode: "review",
    args: ["--target", ".", "--isolated", "--task", "Review"],
    cwd: process.cwd(),
  });
  assert.equal(result.code, 1);
  const env = JSON.parse(result.envelopeText);
  assert.equal(env.status, "failure");
  assert.equal(env.error.class, "isolation-unavailable");
  assert.match(String(env.error.message), /hardened/i);
});

test("adversarial task framing is aggressive", () => {
  const t = buildAdversarialTask("auth");
  assert.match(t, /ADVERSARIAL/);
  assert.match(t, /auth/);
});

test("pretty render shows status and response text", () => {
  const env = tryParseEnvelope(
    JSON.stringify({
      mode: "review",
      status: "success",
      runId: "abc",
      response: { text: "Looks fine." },
    })
  );
  const md = renderEnvelopePretty(env);
  assert.match(md, /Looks fine/);
  assert.match(md, /success/);
});
