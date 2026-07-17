#!/usr/bin/env node
// plugin/scripts/grok-companion.mjs
//
// Entrypoint for every /grok:* skill. Resolves the bundled wrapper (or direct
// Grok CLI), tracks jobs, and adds companion-only commands (result/cancel/jobs/
// transfer/setup extras) while keeping the wrapper as sole author of hardened
// envelopes.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { readAllStdinSync } from "./lib/read-stdin.mjs";
import { resolveWrapperPath, wrapperNotFoundMessage } from "./lib/wrapper.mjs";
import {
  LiveRelay,
  parseRunIdArg,
  parseRunIdMarker,
  renderRunProgress,
  runsDirFor,
  snapshotRunIds,
} from "./progress-relay.mjs";
import {
  appendJobLog,
  createJob,
  formatJobsTable,
  getJob,
  getLastRescueJobId,
  getRunMode,
  listJobs,
  readJobStdout,
  storeJobStdout,
  updateJob,
} from "./lib/jobs.mjs";
import { shouldAttemptTerminalNotify, wrapperChildEnv } from "./lib/notify.mjs";
import {
  maybeNotifyAfterTerminal,
  resolveRunIdFromJobAndStdout,
  sanitizeRunId,
} from "./lib/companion-terminal-notify.mjs";
import { cmdSetup as setupCmd } from "./lib/companion-setup.mjs";
import {
  buildAdversarialTask,
  buildBranchReviewTask,
  buildWorkingTreeReviewTask,
  defaultReviewTarget,
  shortstat,
} from "./lib/git-context.mjs";
import { runDirectGrok } from "./lib/direct-grok.mjs";
import { renderEnvelopePretty, tryParseEnvelope } from "./lib/render.mjs";
import { resolveSpawnedGroupPid, terminateReviewTree } from "./lib/gate-kill.mjs";
import {
  buildTransferTaskBody,
  readSessionStamp,
  resolveTransferSource,
  writeTransferPack,
} from "./lib/session-stamp.mjs";

const PYTHON = process.env.GROK_PYTHON?.trim() || "python3";
const WRAPPER_NOT_FOUND_EXIT = 3;
const SPAWN_FAILED_EXIT = 4;
const SIGNAL_EXIT = 1;
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(SCRIPT_DIR, "..");
// Always bind this process's plugin root before resolving the wrapper. Stale
// CLAUDE_PLUGIN_ROOT from hooks after upgrade must not load an old wrapper tree.
process.env.CLAUDE_PLUGIN_ROOT = PLUGIN_ROOT;
process.env.PLUGIN_ROOT = PLUGIN_ROOT;
const REVIEW_SCHEMA = path.join(PLUGIN_ROOT, "schemas", "review-output.schema.json");

const STREAMING_MODES = new Set(["review", "reason", "code", "adversarial-review"]);
const WRAPPER_MODES = new Set([
  "preflight",
  "review",
  "reason",
  "code",
  "verify",
  "status",
  "cleanup",
  "handoff",
]);

function stderrLine(line) {
  process.stderr.write(`${line}\n`);
}

function stageStdinTaskFile(args) {
  const flagIndex = args.indexOf("--task-file");
  if (flagIndex < 0 || args[flagIndex + 1] !== "-") {
    return null;
  }
  const taskBytes = readAllStdinSync();
  const stagingDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-task-"));
  const taskPath = path.join(stagingDir, "task");
  fs.writeFileSync(taskPath, taskBytes, { mode: 0o600 });
  const staged = args.slice();
  staged[flagIndex + 1] = taskPath;
  const cleanup = () => {
    try {
      fs.rmSync(stagingDir, { recursive: true, force: true });
    } catch (err) {
      stderrLine(`[grok-companion] failed to remove staged task dir ${stagingDir}: ${err.message}`);
    }
  };
  return { args: staged, cleanup };
}

function spawnFailedMessage(wrapper, detail) {
  return (
    `[grok-companion] failed to launch ${PYTHON} ${wrapper}: ${detail}\n` +
    "Fix: ensure python3 is on PATH (set GROK_PYTHON to override), then run /grok:setup.\n"
  );
}

function stripFlags(args) {
  const out = [];
  let pretty = false;
  let runMode = null;
  let jsonOut = false;
  let base = null;
  let resume = false;
  let fresh = false;
  let noNotify = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--pretty") {
      pretty = true;
      continue;
    }
    if (a === "--json") {
      jsonOut = true;
      continue;
    }
    if (a === "--resume") {
      resume = true;
      continue;
    }
    if (a === "--fresh") {
      fresh = true;
      continue;
    }
    // Companion-only: suppress terminal completion notify for this invocation.
    if (a === "--no-notify") {
      noNotify = true;
      continue;
    }
    if (a === "--run-mode" && args[i + 1]) {
      runMode = args[++i];
      continue;
    }
    if (a === "--base" && args[i + 1]) {
      // Captured for review framing; re-attached for code mode later.
      base = args[++i];
      continue;
    }
    out.push(a);
  }
  return { args: out, pretty, runMode, jsonOut, base, resume, fresh, noNotify };
}

function extractTask(args) {
  const tf = args.indexOf("--task-file");
  if (tf >= 0 && args[tf + 1] && args[tf + 1] !== "-") {
    try {
      return fs.readFileSync(args[tf + 1], "utf8");
    } catch {
      return "";
    }
  }
  const t = args.indexOf("--task");
  if (t >= 0 && args[t + 1]) {
    return args[t + 1];
  }
  return "";
}

function injectTaskFile(args, taskText) {
  const cleaned = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--task" || args[i] === "--task-file") {
      i += 1;
      continue;
    }
    cleaned.push(args[i]);
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-task-"));
  const taskPath = path.join(dir, "task");
  fs.writeFileSync(taskPath, taskText, { mode: 0o600 });
  cleaned.push("--task-file", taskPath);
  return {
    args: cleaned,
    cleanup: () => {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        // ignore
      }
    },
  };
}

function ensureTarget(args, cwd) {
  if (args.includes("--target") || args.includes("--worktree")) {
    return { args, cleanup: null };
  }
  const { target } = defaultReviewTarget(cwd);
  return { args: [...args, "--target", target], cleanup: null };
}

function maybeSchema(args) {
  if (args.includes("--schema")) {
    return args;
  }
  if (fs.existsSync(REVIEW_SCHEMA)) {
    return [...args, "--schema", REVIEW_SCHEMA];
  }
  return args;
}

function captureAndTrack(wrapper, args, { cwd, mode, kind, runMode, notifyMode, skipNotify }) {
  const startedAtMs = Date.now();
  // Job registry stores skill mode (e.g. adversarial-review), not wrapper remaps.
  const skillMode = notifyMode || mode;
  const job = createJob(cwd, { kind, mode: skillMode, runMode });
  appendJobLog(cwd, job.id, `dispatch ${args.join(" ")}`);
  stderrLine(`[grok-job] ${job.id} started (${skillMode}, ${runMode})`);

  if (runMode === "direct") {
    const direct = runDirectGrok({ mode, args, cwd, env: process.env });
    storeJobStdout(cwd, job.id, direct.envelopeText);
    updateJob(cwd, job.id, {
      status: direct.code === 0 ? "success" : "failure",
      summary: direct.code === 0 ? "direct grok finished" : "direct grok failed",
    });
    process.stdout.write(direct.envelopeText);
    // Direct has no durable runs/<id> for notified.json; skip push notify.
    return Promise.resolve(direct.code);
  }

  const result = spawnSync(PYTHON, [wrapper, ...args], {
    cwd,
    encoding: "utf8",
    env: wrapperChildEnv(process.env),
    maxBuffer: 64 * 1024 * 1024,
  });

  if (result.error) {
    process.stderr.write(spawnFailedMessage(wrapper, result.error.message));
    updateJob(cwd, job.id, { status: "failure", error: result.error.message });
    return Promise.resolve(SPAWN_FAILED_EXIT);
  }

  if (result.stderr) {
    process.stderr.write(result.stderr);
    for (const line of result.stderr.split("\n")) {
      const runId = parseRunIdMarker(line);
      if (runId && sanitizeRunId(runId)) {
        updateJob(cwd, job.id, { runId });
      }
    }
  }

  const stdout = result.stdout || "";
  if (stdout) {
    process.stdout.write(stdout.endsWith("\n") ? stdout : `${stdout}\n`);
    storeJobStdout(cwd, job.id, stdout);
    const env = tryParseEnvelope(stdout);
    const safe = sanitizeRunId(env?.runId);
    if (safe) {
      updateJob(cwd, job.id, { runId: safe });
    }
  }

  const code = typeof result.status === "number" ? result.status : SIGNAL_EXIT;
  const updated = updateJob(cwd, job.id, {
    status: code === 0 ? "success" : "failure",
    summary: code === 0 ? "completed" : `exit ${code}`,
    pid: null,
  });
  if (!shouldAttemptTerminalNotify({ skipNotify })) {
    return Promise.resolve(code);
  }
  const runId = resolveRunIdFromJobAndStdout(cwd, updated, stdout);
  // Fire-and-forget is wrong for short sync path - wait so process does not exit mid-notify
  // but never throw.
  return maybeNotifyAfterTerminal({
    cwd,
    mode: skillMode,
    runId,
    code,
    startedAtMs,
    stdoutText: stdout,
    stderrLine,
  }).then(() => code);
}

function runPassthrough(wrapper, args) {
  const result = spawnSync(PYTHON, [wrapper, ...args], {
    stdio: "inherit",
    env: wrapperChildEnv(process.env),
  });
  if (result.error) {
    process.stderr.write(spawnFailedMessage(wrapper, result.error.message));
    return SPAWN_FAILED_EXIT;
  }
  if (typeof result.status === "number") {
    return result.status;
  }
  process.stderr.write(
    `[grok-companion] wrapper terminated by signal ${result.signal ?? "unknown"} without an exit code.\n`
  );
  return SIGNAL_EXIT;
}

function runWithLiveRelay(wrapper, args, track) {
  const startedAtMs = Date.now();
  const runsDir = runsDirFor(process.env);
  let knownRunIds;
  try {
    knownRunIds = snapshotRunIds(runsDir);
  } catch (err) {
    stderrLine(`[grok-relay] snapshot failed, continuing without live progress: ${err.message}`);
    knownRunIds = new Set();
  }
  const relay = new LiveRelay({ runsDir, knownRunIds, startMs: startedAtMs, sink: stderrLine });
  const cwd = process.cwd();
  const skillMode = track?.notifyMode || track?.mode || "review";
  const job = track
    ? createJob(cwd, {
        kind: track.kind || "run",
        mode: skillMode,
        runMode: track.runMode || "hardened",
      })
    : null;
  if (job) {
    stderrLine(`[grok-job] ${job.id} started (${skillMode})`);
  }

  return new Promise((resolve) => {
    let settled = false;
    let stdoutBuf = "";
    const finish = (code) => {
      if (settled) return;
      settled = true;
      try {
        relay.stop();
      } catch (err) {
        stderrLine(`[grok-relay] stop failed: ${err.message}`);
      }
      let jobAfter = job;
      if (job) {
        if (stdoutBuf) {
          storeJobStdout(cwd, job.id, stdoutBuf);
        }
        jobAfter = updateJob(cwd, job.id, {
          status: code === 0 ? "success" : "failure",
          summary: code === 0 ? "completed" : `exit ${code}`,
        });
      }
      if (!shouldAttemptTerminalNotify({ skipNotify: track?.skipNotify })) {
        resolve(code);
        return;
      }
      const runId = resolveRunIdFromJobAndStdout(cwd, jobAfter, stdoutBuf);
      maybeNotifyAfterTerminal({
        cwd,
        mode: skillMode,
        runId,
        code,
        startedAtMs,
        stdoutText: stdoutBuf,
        stderrLine,
      }).finally(() => resolve(code));
    };

    let child;
    try {
      // Do NOT detach the python child: the stop-review gate process-group kill
      // targets the companion's group and must reach the wrapper as a descendant.
      child = spawn(PYTHON, [wrapper, ...args], {
        stdio: ["inherit", "pipe", "pipe"],
        env: wrapperChildEnv(process.env),
      });
      if (job && child.pid) {
        updateJob(cwd, job.id, { pid: child.pid, pgid: process.pid });
      }
    } catch (err) {
      process.stderr.write(spawnFailedMessage(wrapper, err.message));
      finish(SPAWN_FAILED_EXIT);
      return;
    }

    if (child.stdout) {
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        process.stdout.write(chunk);
        stdoutBuf += chunk;
      });
    }

    if (child.stderr) {
      let stderrBuffer = "";
      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (chunk) => {
        process.stderr.write(chunk);
        stderrBuffer += chunk;
        let newlineIndex;
        while ((newlineIndex = stderrBuffer.indexOf("\n")) >= 0) {
          const line = stderrBuffer.slice(0, newlineIndex);
          stderrBuffer = stderrBuffer.slice(newlineIndex + 1);
          const runId = parseRunIdMarker(line);
          if (runId && sanitizeRunId(runId)) {
            try {
              relay.adoptRunId(runId);
            } catch (err) {
              stderrLine(`[grok-relay] adopt run id failed: ${err.message}`);
            }
            if (job) {
              updateJob(cwd, job.id, { runId });
            }
          }
        }
      });
    }

    try {
      relay.start();
    } catch (err) {
      stderrLine(`[grok-relay] start failed, continuing without live progress: ${err.message}`);
    }

    child.on("error", (err) => {
      process.stderr.write(spawnFailedMessage(wrapper, err.message));
      finish(SPAWN_FAILED_EXIT);
    });

    child.on("close", (code, signal) => {
      if (typeof code === "number") {
        finish(code);
        return;
      }
      process.stderr.write(
        `[grok-companion] wrapper terminated by signal ${signal ?? "unknown"} without an exit code.\n`
      );
      finish(SIGNAL_EXIT);
    });
  });
}

function runStatus(wrapper, args) {
  // One stdout envelope only. Do NOT re-dump progress to stderr after status:
  // hosts that merge stdout/stderr (Codex terminal) would glue [grok] lines onto
  // the JSON. Progress already lives in response.events / response.target.
  return runPassthrough(wrapper, args);
}

function runHandoff(wrapper, args) {
  // Read-only like status: no job, no live relay, no notify, no Grok spawn.
  // Dual-condition ready is computed inside the wrapper handoff mode.
  return runPassthrough(wrapper, args);
}

function cmdJobs(cwd) {
  process.stdout.write(formatJobsTable(listJobs(cwd)));
  return 0;
}

function cmdResult(cwd, args, pretty) {
  const jobId = args.find((a) => !a.startsWith("--")) || null;
  const job = getJob(cwd, jobId);
  if (!job) {
    process.stderr.write("[grok-companion] no job found. Run a review/code first or pass a job id.\n");
    return 1;
  }
  const raw = readJobStdout(cwd, job.id);
  if (!raw) {
    process.stderr.write(`[grok-companion] job ${job.id} has no stored stdout yet (status=${job.status}).\n`);
    return 1;
  }
  if (pretty) {
    const env = tryParseEnvelope(raw);
    process.stdout.write(env ? renderEnvelopePretty(env) : raw);
    if (!String(raw).endsWith("\n")) process.stdout.write("\n");
  } else {
    process.stdout.write(raw.endsWith("\n") ? raw : `${raw}\n`);
  }
  return job.status === "success" ? 0 : 1;
}

function cmdCancel(cwd, args) {
  const jobId = args.find((a) => !a.startsWith("--")) || null;
  const job = getJob(cwd, jobId);
  if (!job) {
    process.stderr.write("[grok-companion] no job to cancel.\n");
    return 1;
  }
  if (job.status !== "running") {
    process.stdout.write(`Job ${job.id} is already ${job.status}.\n`);
    return 0;
  }
  // Prefer the child pid; pgid was historically the companion pid without setsid.
  const pid = job.pid || job.pgid;
  if (!pid) {
    updateJob(cwd, job.id, {
      status: "failure",
      summary: "cancel failed: no pid recorded; process may still be running",
    });
    process.stdout.write(
      `Job ${job.id}: no live pid recorded; not marked cancelled (process may still be running).\n`
    );
    return 1;
  }
  const isPosix = process.platform !== "win32";
  try {
    terminateReviewTree(pid, isPosix);
  } catch (err) {
    updateJob(cwd, job.id, {
      status: "running",
      summary: `cancel signal failed: ${err.message}`,
    });
    process.stdout.write(`Job ${job.id}: failed to signal tree ${pid}: ${err.message}\n`);
    return 1;
  }
  updateJob(cwd, job.id, { status: "cancelled", summary: "cancelled by operator" });
  process.stdout.write(`Cancelled job ${job.id} (signal tree ${pid}).\n`);
  return 0;
}

function cmdTransfer(cwd, args) {
  let source = null;
  let force = false;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--source" && args[i + 1]) {
      source = args[++i];
    } else if (args[i] === "--force") {
      force = true;
    }
  }
  let sessionPath =
    source ||
    process.env.GROK_CLAUDE_SESSION_PATH ||
    process.env.CLAUDE_SESSION_PATH ||
    "";
  if (!sessionPath) {
    const stamp = readSessionStamp(cwd, process.env);
    if (stamp?.transcript_path) {
      sessionPath = stamp.transcript_path;
    }
  }
  if (!sessionPath) {
    process.stderr.write(
      "[grok-companion] transfer needs a Claude session jsonl.\n" +
        "Pass --source <path> or ensure SessionStart recorded a workspace stamp.\n"
    );
    return 1;
  }
  const resolved = resolveTransferSource(sessionPath, { force, env: process.env });
  if (!resolved.ok) {
    process.stderr.write(`[grok-companion] transfer refused: ${resolved.reason}\n`);
    return 1;
  }
  let body;
  try {
    body = buildTransferTaskBody(resolved.path);
  } catch (err) {
    process.stderr.write(`[grok-companion] could not read session: ${err.message}\n`);
    return 1;
  }
  const taskPath = writeTransferPack(body, process.env);
  process.stdout.write(
    [
      "Transfer pack ready.",
      `session: ${resolved.path}`,
      `task-file: ${taskPath}`,
      "",
      "Continue with:",
      `  node \"$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs\" reason --task-file '${taskPath}'`,
      "or",
      `  node \"$GROK_PLUGIN_ROOT/scripts/grok-companion.mjs\" code --target . --base HEAD --task-file '${taskPath}'`,
      "",
    ].join("\n")
  );
  return 0;
}


function cmdSetup(cwd, args) {
  return setupCmd(cwd, args, { python: PYTHON, pluginRoot: PLUGIN_ROOT });
}


async function cmdDebate(cwd, wrapper, args, runMode) {
  // Bounded two-pass: Grok reason, then a second reason that critiques the first.
  const task = extractTask(args) || "Debate the design tradeoffs in this repository.";
  const round1 = [
    "You are side A in a structured debate. Argue your position clearly with",
    "concrete evidence from the repo or supplied artifacts.",
    "",
    task,
  ].join("\n");
  const inj1 = injectTaskFile(["reason"], round1);
  // Intermediate debate round: no completion notify (final round only).
  const code1 = await captureAndTrack(wrapper, inj1.args, {
    cwd,
    mode: "reason",
    kind: "debate-a",
    runMode,
    skipNotify: true,
  });
  inj1.cleanup();
  if (code1 !== 0) {
    return code1;
  }
  const last = getJob(cwd, null);
  const prior = last ? readJobStdout(cwd, last.id) : "";
  const round2 = [
    "You are side B in a structured debate. Your job is to DISAGREE where",
    "warranted, steelman the other side, and name residual risks.",
    "",
    "## Side A output",
    prior || "(missing)",
    "",
    "## Original topic",
    task,
    "",
    "End with: agreement points, disagreements, and a recommended resolution.",
  ].join("\n");
  const inj2 = injectTaskFile(["reason"], round2);
  const code2 = await captureAndTrack(wrapper, inj2.args, {
    cwd,
    mode: "reason",
    kind: "debate-b",
    runMode,
  });
  inj2.cleanup();
  return code2;
}

function prepareReviewishArgs(mode, args, cwd, base) {
  let next = [...args];
  // map adversarial-review -> review for wrapper
  if (mode === "adversarial-review") {
    next = ["review", ...next.slice(1)];
  }
  const ensured = ensureTarget(next, cwd);
  next = ensured.args;

  let userTask = extractTask(next);
  if (mode === "adversarial-review") {
    userTask = buildAdversarialTask(userTask);
    // grounded-where-it-matters default; --no-web opts out
    if (!hasFlag(next, "--web") && !hasFlag(next, "--no-web")) {
      next.push("--web");
    }
    next = maybeSchema(next);
  } else if (mode === "review") {
    if (base) {
      userTask = buildBranchReviewTask(base, userTask);
    } else if (!userTask.trim()) {
      userTask = buildWorkingTreeReviewTask("");
    }
  }

  if (userTask) {
    // strip old task flags and inject
    const inj = injectTaskFile(next, userTask);
    return { args: inj.args, cleanup: inj.cleanup, wrapperMode: "review" };
  }
  return { args: next, cleanup: null, wrapperMode: mode === "adversarial-review" ? "review" : mode };
}

function hasFlag(args, name) {
  return args.includes(name);
}

function main() {
  const cwd = process.cwd();
  const rawArgs = process.argv.slice(2);
  const {
    args: stripped,
    pretty,
    runMode: runModeFlag,
    base: baseRef,
    resume,
    fresh,
    noNotify,
  } = stripFlags(rawArgs);

  let staged;
  try {
    staged = stageStdinTaskFile(stripped);
  } catch (err) {
    process.stderr.write(
      `[grok-companion] could not stage --task-file from stdin: ${err.message}\n` +
        "Fix: pipe the task on stdin (a single-quoted heredoc) when using --task-file -.\n"
    );
    return SPAWN_FAILED_EXIT;
  }

  const forwardedArgs = staged ? staged.args : stripped;
  const mode = forwardedArgs[0];
  const rest = forwardedArgs.slice(1);

  // No mode: preserve prior behavior (wrapper usage-error envelope).
  if (!mode) {
    const wrapper = resolveWrapperPath(process.env);
    if (!wrapper) {
      process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
      return WRAPPER_NOT_FOUND_EXIT;
    }
    return runPassthrough(wrapper, []);
  }

  // Companion-native commands
  if (mode === "jobs") {
    return cmdJobs(cwd);
  }
  if (mode === "result") {
    return cmdResult(cwd, rest, pretty || rest.includes("--pretty"));
  }
  if (mode === "cancel") {
    return cmdCancel(cwd, rest);
  }
  if (mode === "transfer") {
    return cmdTransfer(cwd, rest);
  }
  if (mode === "setup") {
    return cmdSetup(cwd, rest);
  }
  if (mode === "render") {
    return cmdResult(cwd, rest, true);
  }

  // One-shot --run-mode does NOT persist (adversarial: sticky direct was a trap).
  // Only /grok:setup (cmdSetup -> setRunMode) may write workspace mode.
  const runMode =
    runModeFlag === "direct" || runModeFlag === "hardened" ? runModeFlag : getRunMode(cwd);

  // Rescue resume metadata (skill layer adds flags; we record intent)
  if (mode === "reason" || mode === "code") {
    if (resume && getLastRescueJobId(cwd)) {
      stderrLine(`[grok-companion] --resume: last rescue job was ${getLastRescueJobId(cwd)}`);
    }
    if (fresh) {
      stderrLine("[grok-companion] --fresh: starting a new rescue thread");
    }
  }

  if (mode === "debate") {
    const wrapper = resolveWrapperPath(process.env);
    if (!wrapper && runMode !== "direct") {
      process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
      return WRAPPER_NOT_FOUND_EXIT;
    }
    return cmdDebate(cwd, wrapper, forwardedArgs, runMode);
  }

  let wrapperArgs = forwardedArgs;
  let extraCleanup = null;
  let wrapperMode = mode;

  if (mode === "adversarial-review" || mode === "review") {
    const prepared = prepareReviewishArgs(mode, forwardedArgs, cwd, baseRef);
    wrapperArgs = prepared.args;
    extraCleanup = prepared.cleanup;
    wrapperMode = prepared.wrapperMode;
  }

  // reason defaults web off (wrapper web_defaults); force --no-web when --input is present.
  if (mode === "reason" && hasFlag(wrapperArgs, "--input") && !hasFlag(wrapperArgs, "--web")) {
    if (!hasFlag(wrapperArgs, "--no-web")) {
      wrapperArgs = [...wrapperArgs, "--no-web"];
    }
  }

  if (mode === "code" && baseRef && !wrapperArgs.includes("--base")) {
    wrapperArgs = [...wrapperArgs, "--base", baseRef];
  }

  // Default task for bare review
  if (wrapperMode === "review" && !extractTask(wrapperArgs)) {
    const prepared = injectTaskFile(wrapperArgs, buildWorkingTreeReviewTask(""));
    wrapperArgs = prepared.args;
    const prev = extraCleanup;
    extraCleanup = () => {
      prepared.cleanup();
      if (prev) prev();
    };
  }

  const track = {
    kind: mode === "adversarial-review" ? "adversarial-review" : mode === "code" ? "code" : "run",
    // Keep skill name for notify payload (adversarial-review), wrapperMode for argv.
    mode: wrapperMode,
    notifyMode: mode === "adversarial-review" ? "adversarial-review" : wrapperMode,
    runMode,
    skipNotify: Boolean(noNotify),
  };

  const finishCleanups = (code) => {
    if (extraCleanup) extraCleanup();
    if (staged) staged.cleanup();
    return code;
  };

  // Read-only durable-run modes always use the hardened wrapper (state under
  // XDG runs/), even when workspace prefs are setup --run-mode direct.
  const WRAPPER_ONLY_MODES = new Set(["status", "cleanup", "handoff"]);
  // argparse accepts both "--contract-file PATH" and "--contract-file=PATH".
  const hasContractFile = wrapperArgs.some(
    (a) => a === "--contract-file" || (typeof a === "string" && a.startsWith("--contract-file="))
  );

  if (runMode === "direct") {
    if (wrapperMode === "code" && hasContractFile) {
      process.stderr.write(
        "[grok-companion] --contract-file requires hardened mode (fail closed). " +
          "Run setup --run-mode hardened, or omit --contract-file for direct code.\n"
      );
      return finishCleanups(1);
    }
    if (!WRAPPER_ONLY_MODES.has(wrapperMode)) {
      return Promise.resolve(
        captureAndTrack(null, wrapperArgs, {
          cwd,
          mode: wrapperMode,
          kind: track.kind,
          runMode: "direct",
          notifyMode: track.notifyMode,
          skipNotify: track.skipNotify,
        })
      ).then(finishCleanups);
    }
    // handoff/status/cleanup: fall through to wrapper path below
  }

  const wrapper = resolveWrapperPath(process.env);
  if (!wrapper) {
    process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
    return finishCleanups(WRAPPER_NOT_FOUND_EXIT);
  }

  // status without --run-id: show jobs table first, then wrapper if id present
  if (wrapperMode === "status" && !parseRunIdArg(wrapperArgs)) {
    process.stdout.write(formatJobsTable(listJobs(cwd)));
    process.stdout.write(
      "\nTip: /grok:status --run-id <id> for wrapper envelope; /grok:result [job-id] for stored output.\n"
    );
    return finishCleanups(0);
  }

  if (STREAMING_MODES.has(mode) || STREAMING_MODES.has(wrapperMode)) {
    // Live relay on stderr + capture stdout for /grok:result job store.
    return Promise.resolve(runWithLiveRelay(wrapper, wrapperArgs, track)).then(finishCleanups);
  }

  if (wrapperMode === "status") {
    return finishCleanups(runStatus(wrapper, wrapperArgs));
  }

  if (wrapperMode === "handoff") {
    return finishCleanups(runHandoff(wrapper, wrapperArgs));
  }

  if (WRAPPER_MODES.has(wrapperMode) || wrapperArgs[0]) {
    return Promise.resolve(
      captureAndTrack(wrapper, wrapperArgs, {
        cwd,
        mode: wrapperMode,
        kind: track.kind,
        runMode: "hardened",
        notifyMode: track.notifyMode,
        skipNotify: track.skipNotify,
      })
    ).then(finishCleanups);
  }

  return finishCleanups(runPassthrough(wrapper, wrapperArgs));
}

Promise.resolve()
  .then(main)
  .then((code) => process.exit(typeof code === "number" ? code : 0))
  .catch((err) => {
    process.stderr.write(`[grok-companion] unexpected failure: ${err?.message ?? String(err)}\n`);
    process.exit(SIGNAL_EXIT);
  });
