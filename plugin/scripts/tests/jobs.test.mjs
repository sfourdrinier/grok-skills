import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  createJob,
  formatJobsTable,
  getJob,
  getRunMode,
  listJobs,
  setRunMode,
  storeJobStdout,
  updateJob,
} from "../lib/jobs.mjs";
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
