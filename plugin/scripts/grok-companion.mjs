#!/usr/bin/env node
// plugin/scripts/grok-companion.mjs
//
// Entrypoint for every /grok:* skill. Resolves the bundled wrapper (or direct
// Grok CLI), tracks jobs, and adds companion-only commands (result/cancel/jobs/
// transfer/setup extras) while keeping the wrapper as sole author of hardened
// envelopes.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { extractTask, stageStdinTaskFile, injectTaskFile } from "./lib/task-file.mjs";
import { resolveWrapperPath, wrapperNotFoundMessage } from "./lib/wrapper.mjs";
import {
  LiveRelay,
  parseRunIdArg,
  parseRunIdMarker,
  RUN_ID_RE,
  runsDirFor,
  snapshotRunIds,
} from "./progress-relay.mjs";
import {
  appendJobLog,
  createJob,
  findJobByRunId,
  formatDirectIntegrationConsentMsg,
  formatJobsTable,
  gateIntegrationForCodeish,
  getIntegrationConsent,
  getIntegrationMode,
  getJob,
  getLastRescueJobId,
  getRunMode,
  listJobs,
  parseIntegrationMode,
  readJobStdout,
  resolveJobByIdOrRunId,
  storeJobStdout,
  updateJob,
  withExplicitIntegration,
} from "./lib/jobs.mjs";
import { shouldAttemptTerminalNotify, wrapperChildEnv } from "./lib/notify.mjs";
import { writeHandoffConsumedMarker } from "./subagent-stop-hook.mjs";
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
  parseTargetFlag,
  resolveTargetWorkspaceRoot,
} from "./lib/git-context.mjs";
import {
  isDirectHandoffRequest,
  isDirectRunId,
  runDirectGrok,
  writeDirectNoHandoffRefuse,
} from "./lib/direct-grok.mjs";
import { runAutoIntegrate, runImplementCombo } from "./lib/implement.mjs";
import { stripFlags } from "./lib/companion-args.mjs";
import { renderEnvelopePretty, tryParseEnvelope } from "./lib/render.mjs";
import { terminateReviewTree } from "./lib/gate-kill.mjs";
import {
  ACP_SPEC_POINTER,
  isPeerMode,
  normalizePeerArgs,
  refusePeerDirect,
  runPeerStartBackground,
} from "./lib/peer-acp.mjs";
import { isAcpDisabled, maybeIntegratePeerStop, peerStopExitCode } from "./lib/integrate.mjs";
import { cmdDebate, cmdTransfer } from "./lib/companion-extra-cmds.mjs";
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
  "peer-start",
  "peer-prompt",
  "peer-stop",
]);
function stderrLine(line) {
  process.stderr.write(`${line}\n`);
}
function spawnFailedMessage(wrapper, detail) {
  return (
    `[grok-companion] failed to launch ${PYTHON} ${wrapper}: ${detail}\n` +
    "Fix: ensure python3 is on PATH (set GROK_PYTHON to override), then run /grok:setup.\n"
  );
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
function captureAndTrack(
  wrapper,
  args,
  { cwd, mode, kind, runMode, notifyMode, skipNotify, onStdout }
) {
  const startedAtMs = Date.now();
  // Job registry stores skill mode (e.g. adversarial-review), not wrapper remaps.
  const skillMode = notifyMode || mode;
  const job = createJob(cwd, { kind, mode: skillMode, runMode });
  appendJobLog(cwd, job.id, `dispatch ${args.join(" ")}`);
  stderrLine(`[grok-job] ${job.id} started (${skillMode}, ${runMode})`);
  if (runMode === "direct") {
    const direct = runDirectGrok({
      mode,
      args,
      cwd,
      env: process.env,
      scriptsDir: path.join(PLUGIN_ROOT, "wrapper", "scripts"),
      python: PYTHON,
    });
    storeJobStdout(cwd, job.id, direct.envelopeText);
    const directEnv = tryParseEnvelope(direct.envelopeText);
    const directRunId = isDirectRunId(directEnv?.runId) ? directEnv.runId : null;
    updateJob(cwd, job.id, {
      status: direct.code === 0 ? "success" : "failure",
      summary: direct.code === 0 ? "direct grok finished" : "direct grok failed",
      ...(directRunId ? { runId: directRunId } : {}),
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
  if (typeof onStdout === "function") {
    try {
      onStdout(stdout, code);
    } catch (err) {
      stderrLine(`[grok-companion] onStdout hook failed: ${err.message}`);
    }
  }
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
      // captureStdout: implement/auto need the code envelope buffer + the jobId
      // so the combo can finalize job status + notification AFTER handoff (not on
      // the code-leg exit); others get a number.
      const resolveValue = track?.captureStdout
        ? { code, stdout: stdoutBuf, jobId: jobAfter?.id ?? null }
        : code;
      if (!shouldAttemptTerminalNotify({ skipNotify: track?.skipNotify })) {
        resolve(resolveValue);
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
      }).finally(() => resolve(resolveValue));
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
// status/handoff: one stdout envelope only (no progress re-dump on stderr).
function runStatus(wrapper, args) {
  return runPassthrough(wrapper, args);
}
function runHandoff(wrapper, args) {
  const code = runPassthrough(wrapper, args);
  // On a ready handoff (exit 0), stamp the consumed marker so the SubagentStop
  // fallback stops re-suggesting this run (the marker had no writer before).
  if (code === 0) {
    const i = args.indexOf("--run-id");
    const runId = i >= 0 ? args[i + 1] : undefined;
    if (runId) {
      try {
        writeHandoffConsumedMarker(runId);
      } catch {
        /* best-effort: never fail the handoff over the advisory marker */
      }
    }
  }
  return code;
}
function cmdJobs(cwd) {
  process.stdout.write(formatJobsTable(listJobs(cwd)));
  return 0;
}
function resolveJobArg(cwd, args) {
  const jobId = args.find((a) => !a.startsWith("--")) || null;
  // Direct ids resolve via job index only (never forwarded to the wrapper).
  let job = isDirectRunId(jobId) ? findJobByRunId(cwd, jobId) : null;
  if (!job) job = resolveJobByIdOrRunId(cwd, jobId);
  return { jobId, job };
}
function cmdResult(cwd, args, pretty) {
  const { job } = resolveJobArg(cwd, args);
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
  const { job } = resolveJobArg(cwd, args);
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
function cmdSetup(cwd, args) {
  return setupCmd(cwd, args, { python: PYTHON, pluginRoot: PLUGIN_ROOT });
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
// Post-staging dispatch. Staged stdin cleanup is owned by main()'s finally.
async function dispatch({
  cwd, stripped, pretty, runModeFlag, integrationFlag, baseRef, resume, fresh, noNotify, staged,
}) {
  const forwardedArgs = staged ? staged.args : stripped;
  let mode = forwardedArgs[0];
  let rest = forwardedArgs.slice(1);
  if (!mode) {
    const wrapper = resolveWrapperPath(process.env);
    if (!wrapper) {
      process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
      return WRAPPER_NOT_FOUND_EXIT;
    }
    return runPassthrough(wrapper, []);
  }
  // peer <start|prompt|stop> -> peer-start|peer-prompt|peer-stop
  {
    const normalized = normalizePeerArgs(mode, rest);
    if (normalized.error) {
      process.stderr.write(normalized.error);
      return 1;
    }
    mode = normalized.mode;
    rest = normalized.rest;
  }
  // ACP peer channel is the default; GROK_DISABLE_ACP=1 forces one-shot only.
  if (isPeerMode(mode) && isAcpDisabled(process.env)) {
    process.stderr.write(
      `[grok-companion] peer mode '${mode}' is disabled via GROK_DISABLE_ACP=1 ` +
        `(see ${ACP_SPEC_POINTER}). Unset GROK_DISABLE_ACP to use the ACP peer channel.\n`
    );
    return 1;
  }
  if (mode === "jobs") return cmdJobs(cwd);
  if (mode === "result") return cmdResult(cwd, rest, pretty || rest.includes("--pretty"));
  if (mode === "cancel") return cmdCancel(cwd, rest);
  if (mode === "transfer") return cmdTransfer(cwd, rest);
  // stripFlags peels --integration for the code/implement gate; re-attach for setup.
  if (mode === "setup") {
    const setupArgs =
      integrationFlag != null && String(integrationFlag).trim() !== ""
        ? ["--integration", String(integrationFlag), ...rest]
        : rest;
    return cmdSetup(cwd, setupArgs);
  }
  if (mode === "render") return cmdResult(cwd, rest, true);
  // One-shot --run-mode does NOT persist; only setup may write workspace mode.
  const runMode =
    runModeFlag === "direct" || runModeFlag === "hardened" ? runModeFlag : getRunMode(cwd);
  // Peer channel is hardened-only (private home + worktree + control socket).
  if (isPeerMode(mode) && runMode === "direct") {
    return refusePeerDirect(mode);
  }
  if (mode === "reason" || mode === "code") {
    if (resume && getLastRescueJobId(cwd)) {
      stderrLine(`[grok-companion] --resume: last rescue job was ${getLastRescueJobId(cwd)}`);
    }
    if (fresh) stderrLine("[grok-companion] --fresh: starting a new rescue thread");
  }
  // Integration consent gate (code/implement only). Refuses before wrapper spawn.
  // Re-bind rest so implement/code see the explicit --integration <effective>.
  // Capture effective for auto (apply-on-verified-ready) post-step.
  let integrationEffective = null;
  {
    const gated = gateIntegrationForCodeish(mode, rest, integrationFlag, cwd);
    if (!gated.ok) {
      process.stderr.write(gated.message);
      return gated.code;
    }
    if (gated.effective != null) {
      integrationEffective = gated.effective;
      rest = gated.rest;
    }
  }
  if (mode === "debate") {
    const wrapper = resolveWrapperPath(process.env);
    if (!wrapper && runMode !== "direct") {
      process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
      return WRAPPER_NOT_FOUND_EXIT;
    }
    return cmdDebate(cwd, wrapper, forwardedArgs, runMode, captureAndTrack);
  }

  // implement = code+handoff (no apply). code --integration auto = same + apply.
  const isAutoCode = mode === "code" && integrationEffective === "auto";
  if (mode === "implement" || isAutoCode) {
    if (runMode === "direct") return writeDirectNoHandoffRefuse();
    const wrapper = resolveWrapperPath(process.env);
    if (!wrapper) {
      process.stderr.write(`${wrapperNotFoundMessage(process.env)}\n`);
      return WRAPPER_NOT_FOUND_EXIT;
    }
    const comboRest =
      baseRef && !rest.includes("--base") ? [...rest, "--base", baseRef] : rest;
    const track = {
      kind: "code",
      mode: "code",
      notifyMode: isAutoCode ? "code" : "implement",
      runMode,
      skipNotify: Boolean(noNotify),
    };
    // Finalize job status + fire exactly one terminal notification on the combo's
    // TRUE outcome (after handoff / apply), since the code leg's own notify is
    // suppressed. Prevents /grok:jobs + notifications reporting success for a
    // not-ready implement or a failed auto-apply.
    const comboStartedAtMs = Date.now();
    const finalizeCombo = ({ jobId, finalCode, runId, stdoutText }) => {
      if (jobId) {
        updateJob(cwd, jobId, {
          status: finalCode === 0 ? "success" : "failure",
          summary: finalCode === 0 ? "ready" : `not ready (exit ${finalCode})`,
        });
      }
      if (Boolean(noNotify)) return Promise.resolve();
      return maybeNotifyAfterTerminal({
        cwd,
        mode: track.notifyMode,
        runId,
        code: finalCode,
        startedAtMs: comboStartedAtMs,
        stdoutText: stdoutText || "",
        stderrLine,
      });
    };
    if (isAutoCode) {
      return runAutoIntegrate(wrapper, comboRest, runMode, track, {
        runWithLiveRelay,
        stderrLine,
        targetCwd: cwd,
        finalizeCombo,
      });
    }
    return runImplementCombo(wrapper, comboRest, runMode, track, {
      runWithLiveRelay,
      stderrLine,
      finalizeCombo,
    });
  }

  // review -> wrapper worktree (companion-only modes the wrapper does not accept).
  if (mode === "code" && integrationEffective === "review") {
    rest = withExplicitIntegration(rest, "worktree");
  }
  // Rebuild forwarded args after integration injection into rest (code path).
  let wrapperArgs =
    mode.startsWith("peer-") ? [mode, ...rest] : [mode, ...rest];
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
  if (
    (mode === "code" || mode === "peer-start") &&
    baseRef &&
    !wrapperArgs.includes("--base")
  ) {
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
  // Staged stdin cleanup is owned by main()'s finally - only inject temps here.
  const finishCleanups = (code) => {
    if (extraCleanup) extraCleanup();
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
  // Direct-mode run ids: no hardened run state - refuse before wrapper spawn.
  if (isDirectHandoffRequest(wrapperMode, wrapperArgs)) {
    return finishCleanups(writeDirectNoHandoffRefuse());
  }
  // status <bare-token>: job id and runId share RUN_ID_RE shape. Prefer the
  // workspace job index: known job with recorded runId -> rewrite to THAT
  // runId; known job with no runId -> jobs-table hint and exit 1 (never
  // forward a job id to the wrapper as a run id); unknown token -> --run-id.
  if (wrapperMode === "status" && !parseRunIdArg(wrapperArgs)) {
    const bareIdx = wrapperArgs.findIndex(
      (a, i) => i > 0 && typeof a === "string" && !a.startsWith("-") && RUN_ID_RE.test(a)
    );
    if (bareIdx >= 0) {
      const id = wrapperArgs[bareIdx];
      const knownJob = getJob(cwd, id);
      if (knownJob) {
        const recorded = sanitizeRunId(knownJob.runId);
        if (!recorded) {
          process.stdout.write(formatJobsTable(listJobs(cwd)));
          process.stdout.write(
            "\nTip: /grok:status --run-id <id> for wrapper envelope; /grok:result [job-id] for stored output.\n"
          );
          process.stderr.write(
            `[grok-companion] job ${id} has no recorded runId yet; cannot query wrapper status.\n`
          );
          return finishCleanups(1);
        }
        wrapperArgs = [
          wrapperArgs[0],
          "--run-id",
          recorded,
          ...wrapperArgs.slice(1, bareIdx),
          ...wrapperArgs.slice(bareIdx + 1),
        ];
      } else {
        wrapperArgs = [
          wrapperArgs[0],
          "--run-id",
          id,
          ...wrapperArgs.slice(1, bareIdx),
          ...wrapperArgs.slice(bareIdx + 1),
        ];
      }
    }
  }
  // status without --run-id: show jobs table first, then wrapper if id present
  if (wrapperMode === "status" && !parseRunIdArg(wrapperArgs)) {
    process.stdout.write(formatJobsTable(listJobs(cwd)));
    process.stdout.write(
      "\nTip: /grok:status --run-id <id> for wrapper envelope; /grok:result [job-id] for stored output.\n"
    );
    return finishCleanups(0);
  }
  if (wrapperMode === "peer-start") {
    return Promise.resolve(
      runPeerStartBackground(PYTHON, wrapper, wrapperArgs, {
        spawnFailedMessage,
        signalExit: SIGNAL_EXIT,
        spawnFailedExit: SPAWN_FAILED_EXIT,
      })
    ).then(finishCleanups);
  }
  if (STREAMING_MODES.has(mode) || STREAMING_MODES.has(wrapperMode)) {
    return Promise.resolve(runWithLiveRelay(wrapper, wrapperArgs, track)).then(finishCleanups);
  }
  if (wrapperMode === "status") return finishCleanups(runStatus(wrapper, wrapperArgs));
  if (wrapperMode === "handoff") return finishCleanups(runHandoff(wrapper, wrapperArgs));
  if (WRAPPER_MODES.has(wrapperMode) || wrapperArgs[0]) {
    let peerIntegration = null;
    return Promise.resolve(
      captureAndTrack(wrapper, wrapperArgs, {
        cwd,
        mode: wrapperMode,
        kind: track.kind,
        runMode: "hardened",
        notifyMode: track.notifyMode,
        skipNotify: track.skipNotify,
        onStdout:
          wrapperMode === "peer-stop"
            ? (stdout, code) => {
                if (code === 0) peerIntegration = maybeIntegratePeerStop(stdout, cwd, integrationFlag, rest, stderrLine);
              }
            : null,
      })
    ).then((code) => finishCleanups(peerStopExitCode(code, peerIntegration)));
  }
  return finishCleanups(runPassthrough(wrapper, wrapperArgs));
}
async function main() {
  const cwd = process.cwd();
  const rawArgs = process.argv.slice(2);
  const {
    args: stripped,
    pretty,
    runMode: runModeFlag,
    integration: integrationFlag,
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
  try {
    return await dispatch({
      cwd,
      stripped,
      pretty,
      runModeFlag,
      integrationFlag,
      baseRef,
      resume,
      fresh,
      noNotify,
      staged,
    });
  } finally {
    if (staged) staged.cleanup();
  }
}
Promise.resolve()
  .then(main)
  .then((code) => process.exit(typeof code === "number" ? code : 0))
  .catch((err) => {
    process.stderr.write(`[grok-companion] unexpected failure: ${err?.message ?? String(err)}\n`);
    process.exit(SIGNAL_EXIT);
  });
