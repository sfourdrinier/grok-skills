// plugin/scripts/lib/companion-terminal-notify.mjs
//
// Companion terminal completion notify hook (PR3). Thin wrapper over notify.mjs
// + jobs prefs + run-id path safety. Kept out of grok-companion.mjs for the
// 900-line maintainability cap (AGENTS.md).

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { getJob, getNotificationConfig } from "./jobs.mjs";
import { attemptNotify, NOTIFY_ELIGIBLE_MODES } from "./notify.mjs";
import { tryParseEnvelope } from "./render.mjs";
import { runsDirFor, safeRunIdForRunsDir } from "../progress-relay.mjs";

/**
 * Fail-closed run id for notify/job updates (shape + under runsDir).
 * @param {string|null|undefined} candidate
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null}
 */
export function sanitizeRunId(candidate, env = process.env) {
  return safeRunIdForRunsDir(candidate, runsDirFor(env));
}

/**
 * Thin companion hook: after a terminal live run, at-most-once notify attempt.
 * Never throws; never fails the job. Uses notify.mjs only (DRY).
 *
 * @param {object} opts
 * @param {string} opts.cwd
 * @param {string} opts.mode
 * @param {string|null|undefined} opts.runId
 * @param {number} opts.code
 * @param {number} [opts.startedAtMs]
 * @param {string} [opts.stdoutText]
 * @param {(line: string) => void} [opts.stderrLine]
 * @returns {Promise<void>}
 */
export function maybeNotifyAfterTerminal({
  cwd,
  mode,
  runId,
  code,
  startedAtMs,
  stdoutText,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
}) {
  // Prefer skill/kind mode for notify payload when wrapper mode was remapped
  // (e.g. adversarial-review -> review).
  const notifyMode = NOTIFY_ELIGIBLE_MODES.has(mode) ? mode : null;
  if (!notifyMode) {
    return Promise.resolve();
  }
  const safeRunId = sanitizeRunId(runId, process.env);
  if (!safeRunId) {
    return Promise.resolve();
  }
  const runDir = path.resolve(runsDirFor(process.env), safeRunId);
  if (!fs.existsSync(runDir)) {
    // Direct mode synthetic ids have no durable run dir - skip (no marker home).
    return Promise.resolve();
  }
  const prefs = getNotificationConfig(cwd, process.env);
  // Terminal path only: never advertise lifecycle "running" after process exit.
  let lifecycle = code === 0 ? "completed" : "failed";
  if (stdoutText) {
    const env = tryParseEnvelope(stdoutText);
    if (env?.status === "success") {
      lifecycle = "completed";
    } else if (env?.status === "failure") {
      lifecycle = "failed";
    }
    // ignore status "running" on a finished process - fall back to exit code
  }
  const durationSeconds = Math.max(
    0,
    Math.round((Date.now() - (startedAtMs || Date.now())) / 1000)
  );
  return attemptNotify({
    runDir,
    runId: safeRunId,
    mode: notifyMode,
    lifecycle,
    durationSeconds,
    notificationMode: prefs.notificationMode,
    webhookUrl: prefs.notificationWebhookUrl,
    env: process.env,
  })
    .then((result) => {
      if (result.attempted) {
        stderrLine(
          `[grok-notify] ${result.sent ? "sent" : "failed"} (${result.reason}${
            result.detail ? `: ${result.detail}` : ""
          })`
        );
      }
    })
    .catch((err) => {
      stderrLine(`[grok-notify] swallowed error: ${err.message}`);
    });
}

/**
 * @param {string} cwd
 * @param {object|null|undefined} job
 * @param {string} [stdoutText]
 * @returns {string|null}
 */
export function resolveRunIdFromJobAndStdout(cwd, job, stdoutText) {
  if (job?.runId) {
    const safe = sanitizeRunId(job.runId);
    if (safe) return safe;
  }
  if (stdoutText) {
    const env = tryParseEnvelope(stdoutText);
    const safe = sanitizeRunId(env?.runId);
    if (safe) return safe;
  }
  if (job?.id) {
    const latest = getJob(cwd, job.id);
    return sanitizeRunId(latest?.runId);
  }
  return null;
}
