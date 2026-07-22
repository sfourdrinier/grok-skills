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
  getIntegrationMode,
  getJob,
  getNotificationConfig,
  getRunMode,
  isNotificationMode,
  jobsDir,
  listJobs,
  NOTIFICATION_MODES,
  readJobStdout,
  resolveJobByIdOrRunId,
  setNotificationConfig,
  setRunMode,
  storeJobStdout,
  updateJob,
} from "../lib/jobs.mjs";
import { readGateConfig, resolveStateDir, writeGateConfig } from "../lib/gate-state.mjs";import { runDirectGrok } from "../lib/direct-grok.mjs";
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

test("notification prefs default auto and persist", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-notify-cfg-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "auto");
  assert.equal(DEFAULT_JOBS_CONFIG.notificationMode, "auto");
  setNotificationConfig(cwd, { notificationMode: "off" }, env);
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "off");
  setNotificationConfig(cwd, { notificationMode: "auto" }, env);
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "auto");
});

// --- Task 3.2: CLAUDE_PLUGIN_DATA state root ---

test("stateRoot prefers absolute CLAUDE_PLUGIN_DATA with same workspace keying", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-state-abs-"));
  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  const withData = jobsDir(cwd, env);
  assert.ok(
    withData.startsWith(path.join(pluginData, "state") + path.sep),
    `expected jobs under CLAUDE_PLUGIN_DATA/state, got ${withData}`
  );
  // Workspace segment (slug-hash) must match the legacy layout's trailing segment.
  const legacy = jobsDir(cwd, {});
  const segmentWith = path.basename(path.dirname(withData));
  const segmentLegacy = path.basename(path.dirname(legacy));
  assert.equal(segmentWith, segmentLegacy, "workspace keying must be identical");
  assert.ok(legacy.startsWith(path.join(os.tmpdir(), "grok-companion") + path.sep));
});

test("stateRoot ignores non-absolute CLAUDE_PLUGIN_DATA (fallback unchanged)", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-state-rel-"));
  const env = { CLAUDE_PLUGIN_DATA: "relative-plugin-data" };
  const dir = jobsDir(cwd, env);
  assert.ok(
    dir.startsWith(path.join(os.tmpdir(), "grok-companion") + path.sep),
    `relative CLAUDE_PLUGIN_DATA must fall back to tmp; got ${dir}`
  );
  assert.ok(!dir.includes("relative-plugin-data"));
});

test("stateRoot one-time migrates legacy index+prefs into CLAUDE_PLUGIN_DATA", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-state-mig-"));
  // Seed prefs in the legacy (no CLAUDE_PLUGIN_DATA) location.
  setRunMode(cwd, "direct", {});
  setNotificationConfig(cwd, { notificationMode: "auto" }, {});
  const legacyJobs = jobsDir(cwd, {});
  const legacyRoot = path.dirname(legacyJobs);
  assert.ok(fs.existsSync(path.join(legacyRoot, "jobs-index.json")));

  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  // First touch under new root should migrate and honor setup prefs.
  assert.equal(getRunMode(cwd, env), "direct");
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "auto");
  const newRoot = path.dirname(jobsDir(cwd, env));
  assert.ok(fs.existsSync(path.join(newRoot, "jobs-index.json")));
  assert.ok(newRoot.startsWith(path.join(pluginData, "state")));
  // Legacy left in place (best-effort copy, not move) - frozen snapshot.
  assert.ok(fs.existsSync(path.join(legacyRoot, "jobs-index.json")));
});

// --- Phase 3 review findings 1-3: migration complete-marker + job bodies ---

test("migration retries when new dir exists without jobs-index.json", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-state-mig-retry-"));
  setRunMode(cwd, "direct", {});
  const legacyRoot = path.dirname(jobsDir(cwd, {}));
  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };

  // Pre-create new dir WITHOUT index (partial prior attempt).
  const newRoot = path.dirname(jobsDir(cwd, env));
  // jobsDir may have already migrated; wipe the index to simulate incomplete.
  const indexFile = path.join(newRoot, "jobs-index.json");
  if (fs.existsSync(indexFile)) {
    fs.unlinkSync(indexFile);
  }
  fs.mkdirSync(newRoot, { recursive: true });
  assert.ok(fs.existsSync(newRoot));
  assert.ok(!fs.existsSync(indexFile));

  // Re-seed legacy (migration may have emptied nothing; ensure index still there).
  assert.ok(fs.existsSync(path.join(legacyRoot, "jobs-index.json")));

  // Next touch must re-attempt and complete (index present after).
  assert.equal(getRunMode(cwd, env), "direct");
  assert.ok(fs.existsSync(indexFile), "complete marker is jobs-index.json presence");
});

test("partial migration without index is retried on next call", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-state-mig-partial-"));
  setRunMode(cwd, "direct", {});
  const legacyRoot = path.dirname(jobsDir(cwd, {}));
  const legacyIndex = path.join(legacyRoot, "jobs-index.json");
  assert.ok(fs.existsSync(legacyIndex));

  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  // Compute new root path without triggering full migration success path:
  // create empty dir shell that looks like a failed mid-copy.
  const segment = path.basename(legacyRoot);
  const newRoot = path.join(pluginData, "state", segment);
  fs.mkdirSync(newRoot, { recursive: true });
  // No jobs-index.json -> incomplete; must still migrate.
  assert.ok(!fs.existsSync(path.join(newRoot, "jobs-index.json")));

  assert.equal(getRunMode(cwd, env), "direct");
  assert.ok(
    fs.existsSync(path.join(newRoot, "jobs-index.json")),
    "retry after partial must write jobs-index.json"
  );
  // Legacy remains a frozen snapshot (copy, not move).
  assert.ok(fs.existsSync(legacyIndex));
});

test("migration copies job bodies so stdout is readable under the new root", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-state-mig-bodies-"));
  // Seed a job + stdout under the legacy root (no CLAUDE_PLUGIN_DATA).
  const job = createJob(cwd, { kind: "code", mode: "code", runMode: "hardened" }, {});
  const payload = JSON.stringify({
    status: "success",
    mode: "code",
    response: { text: "migrated-stdout-body" },
  });
  storeJobStdout(cwd, job.id, `${payload}\n`, {});
  const legacyStdout = path.join(jobsDir(cwd, {}), job.id, "stdout.json");
  assert.ok(fs.existsSync(legacyStdout));

  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  // Touch under new root triggers migration of index + job bodies.
  const listed = listJobs(cwd, env);
  assert.equal(listed[0]?.id, job.id);
  const body = readJobStdout(cwd, job.id, env);
  assert.ok(body, "stdout must be readable through the new root after migration");
  assert.match(body, /migrated-stdout-body/);
  // Legacy body left in place (copy not move).
  assert.ok(fs.existsSync(legacyStdout));
});

test("gate-state migrates enabled stop gate into CLAUDE_PLUGIN_DATA root", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-mig-"));
  fs.mkdirSync(path.join(cwd, ".git"));
  // Enable gate under legacy tmp root (no CLAUDE_PLUGIN_DATA).
  writeGateConfig(cwd, true, {});
  assert.equal(readGateConfig(cwd, {}).stopReviewGate, true);
  const legacyDir = resolveStateDir(cwd, {});
  assert.ok(fs.existsSync(path.join(legacyDir, "gate-state.json")));

  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  // Enabling under tmp must not be lost when CLAUDE_PLUGIN_DATA appears.
  assert.equal(readGateConfig(cwd, env).stopReviewGate, true);
  const newDir = resolveStateDir(cwd, env);
  assert.ok(newDir.startsWith(path.join(pluginData, "state")));
  assert.ok(fs.existsSync(path.join(newDir, "gate-state.json")));
  // Legacy frozen snapshot (copy not move).
  assert.ok(fs.existsSync(path.join(legacyDir, "gate-state.json")));
});

test("gate-state migration retries when new dir exists without gate-state.json", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-mig-retry-"));
  fs.mkdirSync(path.join(cwd, ".git"));
  writeGateConfig(cwd, true, {});
  const legacyDir = resolveStateDir(cwd, {});
  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  const segment = path.basename(legacyDir);
  const newDir = path.join(pluginData, "state", segment);
  fs.mkdirSync(newDir, { recursive: true });
  assert.ok(!fs.existsSync(path.join(newDir, "gate-state.json")));

  assert.equal(readGateConfig(cwd, env).stopReviewGate, true);
  assert.ok(fs.existsSync(path.join(newDir, "gate-state.json")));
});// --- Task 3.4: userConfig env defaults (CLAUDE_PLUGIN_OPTION_*) ---

test("getRunMode precedence: setup > CLAUDE_PLUGIN_OPTION_RUNMODE > default", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-opt-run-"));
  const pluginData = path.join(cwd, "pdata");

  // Default when neither setup nor option is set.
  assert.equal(getRunMode(cwd, { CLAUDE_PLUGIN_DATA: pluginData }), "hardened");

  // userConfig env alone (key uppercased: runMode -> RUNMODE).
  assert.equal(
    getRunMode(cwd, {
      CLAUDE_PLUGIN_DATA: pluginData,
      CLAUDE_PLUGIN_OPTION_RUNMODE: "direct",
    }),
    "direct"
  );

  // Underscore form also accepted (host/docs ambiguity; trivially cheap).
  assert.equal(
    getRunMode(cwd, {
      CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata2"),
      CLAUDE_PLUGIN_OPTION_RUN_MODE: "direct",
    }),
    "direct"
  );

  // Setup wins over CLAUDE_PLUGIN_OPTION_*.
  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata3"),
    CLAUDE_PLUGIN_OPTION_RUNMODE: "hardened",
  };
  setRunMode(cwd, "direct", env);
  assert.equal(getRunMode(cwd, env), "direct");
});

test("getRunMode ignores invalid CLAUDE_PLUGIN_OPTION_RUNMODE with stderr note", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-opt-bad-"));
  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata"),
    CLAUDE_PLUGIN_OPTION_RUNMODE: "turbo",
  };
  const prevErr = process.stderr.write;
  let err = "";
  process.stderr.write = (chunk, ...rest) => {
    err += String(chunk);
    return prevErr.call(process.stderr, chunk, ...rest);
  };
  try {
    assert.equal(getRunMode(cwd, env), "hardened");
    assert.match(err, /CLAUDE_PLUGIN_OPTION_RUNMODE|ignoring invalid/i);
  } finally {
    process.stderr.write = prevErr;
  }
});

test("getNotificationConfig precedence: setup > CLAUDE_PLUGIN_OPTION_* > default", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-opt-notify-"));
  const pluginData = path.join(cwd, "pdata");

  assert.equal(
    getNotificationConfig(cwd, { CLAUDE_PLUGIN_DATA: pluginData }).notificationMode,
    "auto"
  );

  const fromEnv = getNotificationConfig(cwd, {
    CLAUDE_PLUGIN_DATA: pluginData,
    CLAUDE_PLUGIN_OPTION_NOTIFICATIONMODE: "webhook",
    CLAUDE_PLUGIN_OPTION_NOTIFICATIONWEBHOOKURL: "https://hooks.example.com/x",
  });
  assert.equal(fromEnv.notificationMode, "webhook");
  assert.equal(fromEnv.notificationWebhookUrl, "https://hooks.example.com/x");

  // Setup wins.
  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata2"),
    CLAUDE_PLUGIN_OPTION_NOTIFICATIONMODE: "native",
    CLAUDE_PLUGIN_OPTION_NOTIFICATIONWEBHOOKURL: "https://hooks.example.com/y",
  };
  setNotificationConfig(
    cwd,
    { notificationMode: "auto", notificationWebhookUrl: "https://hooks.example.com/setup" },
    env
  );
  const afterSetup = getNotificationConfig(cwd, env);
  assert.equal(afterSetup.notificationMode, "auto");
  assert.equal(afterSetup.notificationWebhookUrl, "https://hooks.example.com/setup");
});

test("getNotificationConfig ignores invalid CLAUDE_PLUGIN_OPTION values with note", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-opt-nbad-"));
  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata"),
    CLAUDE_PLUGIN_OPTION_NOTIFICATIONMODE: "telepathy",
    CLAUDE_PLUGIN_OPTION_NOTIFICATIONWEBHOOKURL: "not-a-url",
  };
  const prevErr = process.stderr.write;
  let err = "";
  process.stderr.write = (chunk, ...rest) => {
    err += String(chunk);
    return prevErr.call(process.stderr, chunk, ...rest);
  };
  try {
    const cfg = getNotificationConfig(cwd, env);
    assert.equal(cfg.notificationMode, "auto");
    assert.equal(cfg.notificationWebhookUrl, null);
    assert.match(err, /ignoring invalid/i);
  } finally {
    process.stderr.write = prevErr;
  }
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

// Phase 1 finding 3: job id and runId share YYYYMMDDTHHMMSSZ-xxxxxx shape.
// Exact job-id match must win over runId lookup on collision.
test("resolveJobByIdOrRunId prefers exact job-id match over runId collision", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-jobs-collision-"));
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  // Job A id === job B runId (same shape). Prefer A when looking up that token.
  const shared = "20260716T120000Z-aaaaaa";
  const jobA = createJob(
    cwd,
    { id: shared, kind: "review", mode: "review", runMode: "hardened" },
    env
  );
  updateJob(cwd, jobA.id, { status: "success", summary: "job-A" }, env);
  storeJobStdout(
    cwd,
    jobA.id,
    JSON.stringify({ status: "success", response: { text: "from-job-A" } }) + "\n",
    env
  );
  const jobB = createJob(cwd, { kind: "code", mode: "code", runMode: "hardened" }, env);
  updateJob(cwd, jobB.id, { runId: shared, status: "success", summary: "job-B" }, env);
  storeJobStdout(
    cwd,
    jobB.id,
    JSON.stringify({ status: "success", response: { text: "from-job-B" } }) + "\n",
    env
  );

  const resolved = resolveJobByIdOrRunId(cwd, shared, env);
  assert.ok(resolved, "expected a job");
  assert.equal(resolved.id, jobA.id, "exact job-id match must win over runId");
  assert.equal(resolved.summary, "job-A");

  // result / cancel go through the same resolver
  const { env: fakeEnv, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["result", shared], {
      cwd,
      env: { ...fakeEnv, CLAUDE_PLUGIN_DATA: env.CLAUDE_PLUGIN_DATA },
    });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.match(res.stdout, /from-job-A/);
    assert.ok(!res.stdout.includes("from-job-B"), "must not return colliding runId job");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("status known job id rewrites to that job's runId", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-status-jobid-"));
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  const jobId = "20260716T130000Z-bbbbbb";
  const runId = "20260716T140000Z-cccccc";
  const job = createJob(
    cwd,
    { id: jobId, kind: "code", mode: "code", runMode: "hardened" },
    envBase
  );
  updateJob(cwd, job.id, { runId, status: "success" }, envBase);

  // {{RUN_ID}} is substituted with the --run-id the companion actually forwarded.
  const { env, cleanup } = makeFakeWrapper({
    status: {
      stdout: JSON.stringify({ status: "success", runId: "{{RUN_ID}}", mode: "status" }),
      exitCode: 0,
    },
  });
  try {
    const res = runCompanion(["status", jobId], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: pluginData },
    });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.match(res.stdout, new RegExp(runId));
    assert.ok(!res.stdout.includes(jobId), "must forward recorded runId, not job id");
    const parsed = JSON.parse(res.stdout.trim());
    assert.equal(parsed.runId, runId, "wrapper must receive the job's recorded runId");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("status known job id without runId prints jobs table and exits 1", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-status-no-runid-"));
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  const jobId = "20260716T150000Z-dddddd";
  createJob(cwd, { id: jobId, kind: "code", mode: "code", runMode: "hardened" }, envBase);
  // No runId recorded.

  const { env, cleanup } = makeFakeWrapper({
    // If status were wrongly forwarded with the job id as --run-id, this would run.
    status: { stdout: "{}", exitCode: 0 },
  });
  try {
    const res = runCompanion(["status", jobId], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: pluginData },
    });
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr} stdout: ${res.stdout}`);
    // Jobs table hint (formatJobsTable header or tip line)
    assert.match(res.stdout, /ID\s+KIND|Tip:|No Grok jobs|code/);
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "must not forward job id without runId to wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});


test("legacy jobs-index notificationMode off is not setup-authored after default flip", () => {
  // Pre-2.0.1 indexes persisted off without prefsSources. That must not pin
  // off as setup so the new auto default / CLAUDE_PLUGIN_OPTION still apply.
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-legacy-notify-"));
  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  // Force state root creation, then overwrite with a legacy-shaped index.
  createJob(cwd, { kind: "review", mode: "review", runMode: "hardened" }, env);
  const jobs = listJobs(cwd, env);
  assert.ok(jobs.length >= 1);
  // Locate jobs-index.json under plugin data state tree.
  function findIndex(dir) {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, ent.name);
      if (ent.isFile() && ent.name === "jobs-index.json") return full;
      if (ent.isDirectory()) {
        const hit = findIndex(full);
        if (hit) return hit;
      }
    }
    return null;
  }
  const indexPath = findIndex(pluginData);
  assert.ok(indexPath, "expected jobs-index.json under plugin data");
  const legacy = {
    version: 1,
    config: {
      runMode: "hardened",
      notificationMode: "off",
      notificationWebhookUrl: null,
      // no prefsSources => legacySetup path
    },
    jobs: [],
  };
  fs.writeFileSync(indexPath, JSON.stringify(legacy), "utf8");
  assert.equal(getNotificationConfig(cwd, env).notificationMode, "auto");
  // Explicit userConfig must still win over unpinned legacy off.
  assert.equal(
    getNotificationConfig(cwd, {
      ...env,
      CLAUDE_PLUGIN_OPTION_NOTIFICATIONMODE: "native",
    }).notificationMode,
    "native"
  );
});

test("legacy jobs-index non-default integrationMode is pinned as setup", () => {
  // Codex PR #9: pre-prefsSources index with setup --integration worktree/auto/
  // review must not fall through to built-in direct after consent became a no-op.
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-legacy-integ-"));
  const pluginData = path.join(cwd, "pdata");
  const env = { CLAUDE_PLUGIN_DATA: pluginData };
  createJob(cwd, { kind: "code", mode: "code", runMode: "hardened" }, env);
  function findIndex(dir) {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, ent.name);
      if (ent.isFile() && ent.name === "jobs-index.json") return full;
      if (ent.isDirectory()) {
        const hit = findIndex(full);
        if (hit) return hit;
      }
    }
    return null;
  }
  const indexPath = findIndex(pluginData);
  assert.ok(indexPath, "expected jobs-index.json under plugin data");

  for (const mode of ["worktree", "auto", "review"]) {
    fs.writeFileSync(
      indexPath,
      JSON.stringify({
        version: 1,
        config: {
          runMode: "hardened",
          notificationMode: "off",
          integrationMode: mode,
          // no prefsSources => legacySetup path
        },
        jobs: [],
      }),
      "utf8"
    );
    assert.equal(getIntegrationMode(cwd, env), mode, `legacy ${mode} must stick`);
    // userConfig must not override setup-authored legacy non-default mode.
    assert.equal(
      getIntegrationMode(cwd, {
        ...env,
        CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "direct",
      }),
      mode,
      `userConfig must not demote legacy ${mode} to direct`
    );
  }

  // Legacy default "direct" stays unpinned so userConfig / built-in apply.
  fs.writeFileSync(
    indexPath,
    JSON.stringify({
      version: 1,
      config: {
        runMode: "hardened",
        integrationMode: "direct",
      },
      jobs: [],
    }),
    "utf8"
  );
  assert.equal(getIntegrationMode(cwd, env), "direct");
  assert.equal(
    getIntegrationMode(cwd, {
      ...env,
      CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "review",
    }),
    "review"
  );
});

