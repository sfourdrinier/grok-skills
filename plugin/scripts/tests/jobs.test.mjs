import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  createJob,
  DEFAULT_JOBS_CONFIG,
  findJobByRunId,
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
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

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

test("findJobByRunId resolves the newest job carrying that runId", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-jobs-runid-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const runId = "20260716T000000Z-abc123";
  // Older job also tagged with the same runId later updated - newest wins via index order.
  const older = createJob(cwd, { kind: "review", mode: "review", runMode: "hardened" }, env);
  updateJob(cwd, older.id, { runId, status: "success", summary: "older" }, env);
  const newer = createJob(cwd, { kind: "code", mode: "code", runMode: "hardened" }, env);
  updateJob(cwd, newer.id, { runId, status: "success", summary: "newer" }, env);

  const found = findJobByRunId(cwd, runId, env);
  assert.ok(found, "expected a job for known runId");
  assert.equal(found.id, newer.id);
  assert.equal(found.summary, "newer");
  assert.equal(findJobByRunId(cwd, "20990101T000000Z-ffffff", env), null);
  assert.equal(findJobByRunId(cwd, "", env), null);
  assert.equal(findJobByRunId(cwd, null, env), null);
});

test("result <runId> returns stored stdout via companion", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-result-runid-"));
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  const runId = "20260716T120000Z-deadbe";
  const job = createJob(cwd, { kind: "review", mode: "review", runMode: "hardened" }, envBase);
  updateJob(cwd, job.id, { runId, status: "success" }, envBase);
  const payload = JSON.stringify({
    status: "success",
    mode: "review",
    runId,
    response: { text: "stored-by-runId" },
  });
  storeJobStdout(cwd, job.id, `${payload}\n`, envBase);

  const { env: fakeEnv, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["result", runId], {
      cwd,
      env: { ...fakeEnv, CLAUDE_PLUGIN_DATA: pluginData },
    });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.match(res.stdout, /stored-by-runId/);
    assert.match(res.stdout, new RegExp(runId));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("status bare runId positional rewrites to --run-id before wrapper", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-status-bare-"));
  const runId = "20260716T000000Z-abc123";
  const envelope = JSON.stringify({ status: "success", runId, mode: "status" });
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: envelope, exitCode: 0 },
  });
  try {
    const res = runCompanion(["status", runId], { env, cwd });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.ok(res.stdout.includes(envelope), `stdout missing envelope: ${res.stdout}`);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
