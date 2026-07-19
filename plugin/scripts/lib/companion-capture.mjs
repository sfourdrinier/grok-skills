// plugin/scripts/lib/companion-capture.mjs
//
// Hardened/direct capture+track completion path extracted from grok-companion.mjs
// (900-line cap). One stdout envelope: optional onStdout may rewrite the envelope
// and effective code BEFORE first write / store / notify (peer-stop apply honesty).

import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";

import {
  maybeNotifyAfterTerminal,
  resolveRunIdFromJobAndStdout,
  sanitizeRunId,
} from "./companion-terminal-notify.mjs";
import { isDirectRunId, runDirectGrok } from "./direct-grok.mjs";
import {
  appendJobLog,
  createJob,
  storeJobStdout,
  updateJob,
} from "./jobs.mjs";
import { shouldAttemptTerminalNotify, wrapperChildEnv } from "./notify.mjs";
import { tryParseEnvelope } from "./render.mjs";
import { parseRunIdMarker } from "../progress-relay.mjs";

/**
 * @param {object} deps
 * @param {string} deps.python
 * @param {string} deps.pluginRoot
 * @param {number} deps.spawnFailedExit
 * @param {number} deps.signalExit
 * @param {(wrapper: string, detail: string) => string} deps.spawnFailedMessage
 * @param {(line: string) => void} deps.stderrLine
 * @returns {(wrapper: string|null, args: string[], opts: object) => Promise<number>}
 */
export function createCaptureAndTrack({
  python,
  pluginRoot,
  spawnFailedExit,
  signalExit,
  spawnFailedMessage,
  stderrLine,
}) {
  return function captureAndTrack(
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
        scriptsDir: path.join(pluginRoot, "wrapper", "scripts"),
        python,
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
    const result = spawnSync(python, [wrapper, ...args], {
      cwd,
      encoding: "utf8",
      env: wrapperChildEnv(process.env),
      maxBuffer: 64 * 1024 * 1024,
    });
    if (result.error) {
      process.stderr.write(spawnFailedMessage(wrapper, result.error.message));
      updateJob(cwd, job.id, { status: "failure", error: result.error.message });
      return Promise.resolve(spawnFailedExit);
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
    const rawStdout = result.stdout || "";
    const code = typeof result.status === "number" ? result.status : signalExit;
    // Capture wrapper output first. onStdout (e.g. peer-stop apply) may rewrite the
    // envelope + effective code BEFORE first stdout write / store / notify so a
    // blocked apply never looks like a successful ready peer-stop.
    // Hook throws synthesize ONE complete failure envelope (applied=false,
    // integration-error), nonzero code - never leave raw ready success on
    // stdout/store/notify.
    let emitStdout = rawStdout;
    let effectiveCode = code;
    if (typeof onStdout === "function") {
      try {
        const hookResult = onStdout(rawStdout, code);
        if (typeof hookResult === "number") {
          effectiveCode = hookResult;
        } else if (hookResult && typeof hookResult === "object") {
          if (typeof hookResult.code === "number") effectiveCode = hookResult.code;
          if (typeof hookResult.stdoutText === "string") emitStdout = hookResult.stdoutText;
        }
      } catch (err) {
        stderrLine(`[grok-companion] onStdout hook failed: ${err.message}`);
        effectiveCode = 1;
        const raw = tryParseEnvelope(rawStdout);
        const base =
          raw && typeof raw === "object"
            ? { ...raw }
            : {
                schemaVersion: 1,
                mode: mode || "run",
                status: "failure",
                runId: null,
                response: null,
              };
        const baseResp =
          base.response && typeof base.response === "object" ? base.response : {};
        const baseInteg =
          baseResp.integration && typeof baseResp.integration === "object"
            ? baseResp.integration
            : {};
        const failEnv = {
          ...base,
          schemaVersion: typeof base.schemaVersion === "number" ? base.schemaVersion : 1,
          status: "failure",
          error: {
            class: "integration-error",
            message: `onStdout hook failed: ${err.message}`,
          },
          response: {
            ...baseResp,
            integration: {
              ...baseInteg,
              applied: false,
              ready: false,
              outcome: "integration-error",
            },
          },
        };
        emitStdout = `${JSON.stringify(failEnv)}\n`;
      }
    }
    if (emitStdout) {
      process.stdout.write(emitStdout.endsWith("\n") ? emitStdout : `${emitStdout}\n`);
      storeJobStdout(cwd, job.id, emitStdout);
      const env = tryParseEnvelope(emitStdout);
      const safe = sanitizeRunId(env?.runId);
      if (safe) {
        updateJob(cwd, job.id, { runId: safe });
      }
    }
    const updated = updateJob(cwd, job.id, {
      status: effectiveCode === 0 ? "success" : "failure",
      summary: effectiveCode === 0 ? "completed" : `exit ${effectiveCode}`,
      pid: null,
    });
    if (!shouldAttemptTerminalNotify({ skipNotify })) {
      return Promise.resolve(effectiveCode);
    }
    const runId = resolveRunIdFromJobAndStdout(cwd, updated, emitStdout);
    // Wait so process does not exit mid-notify; never throw. Use FINAL envelope +
    // effective code so blocked peer-stop notifies failed, not completed.
    return maybeNotifyAfterTerminal({
      cwd,
      mode: skillMode,
      runId,
      code: effectiveCode,
      startedAtMs,
      stdoutText: emitStdout,
      stderrLine,
    }).then(() => effectiveCode);
  };
}
