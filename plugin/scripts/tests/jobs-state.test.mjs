// plugin/scripts/tests/jobs-state.test.mjs
//
// F02: prefs are not the jobs array; corrupt reads must not restore live-tree
// direct; running jobs survive the history cap; codexAgentsScope lives in
// canonical prefs (sidecar is not required after a job write).

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { getCodexAgentsScope, setCodexAgentsScope } from "../lib/codex-agents.mjs";
import {
  createJob,
  getIntegrationMode,
  getNotificationConfig,
  jobsDir,
  listJobs,
  setIntegrationMode,
  setNotificationConfig,
  setStoredCodexAgentsScope,
  updateJob,
} from "../lib/jobs.mjs";

const LIB_DIR = path.dirname(fileURLToPath(import.meta.url));

function stateRootFromJobs(cwd, env) {
  return path.dirname(jobsDir(cwd, env));
}

function indexPath(cwd, env) {
  return path.join(stateRootFromJobs(cwd, env), "jobs-index.json");
}

test("corrupt jobs-index does not reset setup integrationMode to direct", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prefs-corrupt-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(setIntegrationMode(cwd, "review", env), "review");
  assert.equal(getIntegrationMode(cwd, env), "review");
  fs.writeFileSync(indexPath(cwd, env), "{not-json", { encoding: "utf8", mode: 0o600 });
  assert.equal(getIntegrationMode(cwd, env), "review");
});

test("garbage setup-authored integrationMode in prefs uses bak, not live direct", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prefs-garbage-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(setIntegrationMode(cwd, "review", env), "review");
  const root = stateRootFromJobs(cwd, env);
  const prefs = path.join(root, "prefs.json");
  const bak = path.join(root, "prefs.json.bak");
  assert.equal(fs.existsSync(bak), true, "last-known-good bak must exist after setup write");
  fs.writeFileSync(
    prefs,
    JSON.stringify({
      version: 2,
      config: {
        integrationMode: "turbo",
        prefsSources: { integrationMode: "setup" },
      },
    }),
    { encoding: "utf8", mode: 0o600 }
  );
  assert.equal(getIntegrationMode(cwd, env), "review");
  createJob(cwd, { kind: "run", mode: "code" }, env);
  assert.equal(getIntegrationMode(cwd, env), "review");
});

test("running jobs remain listed after more than 50 later jobs", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-jobs-cap-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const first = createJob(cwd, { kind: "run", mode: "code", runMode: "hardened" }, env);
  for (let i = 0; i < 50; i++) {
    const job = createJob(cwd, { kind: "run", mode: "review", runMode: "hardened" }, env);
    updateJob(cwd, job.id, { status: "success", summary: "done" }, env);
  }
  const listed = listJobs(cwd, env);
  assert.ok(
    listed.some((j) => j.id === first.id && j.status === "running"),
    "active execution handle must not be evicted by the history cap"
  );
});

test("codexAgentsScope survives a later job write without the sidecar", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-scope-persist-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(setCodexAgentsScope(cwd, "project", env), "project");
  createJob(cwd, { kind: "run", mode: "code" }, env);
  const sidecar = path.join(stateRootFromJobs(cwd, env), "codex-agents-prefs.json");
  if (fs.existsSync(sidecar)) fs.unlinkSync(sidecar);
  assert.equal(getCodexAgentsScope(cwd, env), "project");
});

test("concurrent creates in one workspace keep every job", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-jobs-conc-"));
  const envDir = path.join(cwd, "pdata");
  const worker = path.resolve(LIB_DIR, "../lib/jobs.mjs");
  const n = 20;
  const children = [];
  for (let i = 0; i < n; i++) {
    children.push(
      new Promise((resolve, reject) => {
        const child = spawn(
          process.execPath,
          [
            "--input-type=module",
            "-e",
            `import { createJob } from ${JSON.stringify(worker)};
             createJob(process.env.JOB_CWD, { kind: "run", mode: "code" }, {
               CLAUDE_PLUGIN_DATA: process.env.JOB_DATA,
             });`,
          ],
          {
            env: {
              ...process.env,
              JOB_CWD: cwd,
              JOB_DATA: envDir,
            },
            stdio: ["ignore", "ignore", "pipe"],
          }
        );
        let err = "";
        child.stderr.on("data", (chunk) => {
          err += chunk;
        });
        child.on("exit", (code) => {
          if (code === 0) resolve();
          else reject(new Error(`worker exit ${code}: ${err}`));
        });
      })
    );
  }
  await Promise.all(children);
  const listed = listJobs(cwd, { CLAUDE_PLUGIN_DATA: envDir });
  assert.equal(listed.length, n);
});

test("job bookkeeping does not republish a stale integrationMode", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prefs-stale-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(setIntegrationMode(cwd, "direct", env), "direct");
  const job = createJob(cwd, { kind: "run", mode: "code" }, env);
  assert.equal(setIntegrationMode(cwd, "review", env), "review");
  updateJob(cwd, job.id, { status: "success", summary: "done" }, env);
  assert.equal(getIntegrationMode(cwd, env), "review");
});

test("independent preference mutations survive a later job update", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prefs-indep-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(setIntegrationMode(cwd, "worktree", env), "worktree");
  setNotificationConfig(cwd, { notificationMode: "off" }, env);
  setStoredCodexAgentsScope(cwd, "project", env);
  const job = createJob(cwd, { kind: "run", mode: "code" }, env);
  updateJob(cwd, job.id, { pid: 99 }, env);
  assert.equal(getIntegrationMode(cwd, env), "worktree");
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "off");
  assert.equal(getCodexAgentsScope(cwd, env), "project");
});

test("unusable existing prefs plus backup do not fall back to live direct", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prefs-loss-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(setIntegrationMode(cwd, "review", env), "review");
  const root = stateRootFromJobs(cwd, env);
  fs.writeFileSync(path.join(root, "prefs.json"), "{not-json", {
    encoding: "utf8",
    mode: 0o600,
  });
  fs.writeFileSync(path.join(root, "prefs.json.bak"), JSON.stringify({ config: {} }), {
    encoding: "utf8",
    mode: 0o600,
  });
  assert.notEqual(getIntegrationMode(cwd, env), "direct");
});

test("cancel_failed then observed success becomes terminal success", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-then-ok-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(cwd, { kind: "run", mode: "code" }, env);
  updateJob(cwd, job.id, { status: "cancel_failed", summary: "EPERM" }, env);
  const next = updateJob(cwd, job.id, { status: "success", summary: "finished" }, env);
  assert.equal(next.status, "success");
});

test("confirmed cancelled ignores a delayed success callback", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cancel-keep-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const job = createJob(cwd, { kind: "run", mode: "code" }, env);
  updateJob(cwd, job.id, { status: "cancelled", summary: "cancelled by operator" }, env);
  const next = updateJob(cwd, job.id, { status: "success", summary: "late" }, env);
  assert.equal(next.status, "cancelled");
});
