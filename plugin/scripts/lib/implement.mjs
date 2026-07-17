// plugin/scripts/lib/implement.mjs
//
// One-call implement combo: code (live relay) then handoff verification.
// Exit 0 only when code succeeded AND handoff is dual-condition ready.
// Handoff still runs after failed code when a runId exists (surface blockers).
// Direct-mode refusal reuses DIRECT_NO_HANDOFF_MSG from direct-grok.mjs.

import { spawnSync } from "node:child_process";

import { sanitizeRunId } from "./companion-terminal-notify.mjs";
import { DIRECT_NO_HANDOFF_MSG, writeDirectNoHandoffRefuse } from "./direct-grok.mjs";
import { wrapperChildEnv } from "./notify.mjs";
import { tryParseEnvelope } from "./render.mjs";

export { DIRECT_NO_HANDOFF_MSG };

/**
 * Capture handoff stdout so implement can read response.integration.ready.
 * Relays stderr + stdout like a passthrough; returns parsed envelope.
 */
export function runHandoffCaptured(wrapper, args, {
  python = process.env.GROK_PYTHON?.trim() || "python3",
  spawnFailedExit = 4,
  signalExit = 1,
  spawnFailedMessage = (w, d) =>
    `[grok-companion] failed to launch ${python} ${w}: ${d}\n`,
} = {}) {
  const result = spawnSync(python, [wrapper, ...args], {
    encoding: "utf8",
    env: wrapperChildEnv(process.env),
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) {
    process.stderr.write(spawnFailedMessage(wrapper, result.error.message));
    return { code: spawnFailedExit, envelope: null };
  }
  if (result.stderr) process.stderr.write(result.stderr);
  const stdout = result.stdout || "";
  if (stdout) process.stdout.write(stdout.endsWith("\n") ? stdout : `${stdout}\n`);
  return {
    code: typeof result.status === "number" ? result.status : signalExit,
    envelope: tryParseEnvelope(stdout),
  };
}

/**
 * One-call implement: code (live relay) then handoff.
 * Exit 0 only when code exit 0 AND handoff exit 0 AND
 * response.integration.ready === true; exit 1 on any other outcome.
 * When a runId is present, handoff always runs (even after failed code) so
 * not-ready blockers surface. Without a runId, returns 1 (never raw spawn code).
 * Direct mode is refused before any wrapper spawn.
 */
export async function runImplementCombo(wrapper, rest, runMode, track, {
  runWithLiveRelay,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
} = {}) {
  if (runMode === "direct") {
    return writeDirectNoHandoffRefuse();
  }
  const codeArgs = ["code", ...rest];
  const res = await runWithLiveRelay(wrapper, codeArgs, { ...track, captureStdout: true });
  const code = typeof res === "number" ? res : res.code;
  const stdoutBuf = typeof res === "number" ? "" : res.stdout || "";
  const env = tryParseEnvelope(stdoutBuf);
  const runId = sanitizeRunId(env?.runId);
  if (!runId) {
    process.stderr.write(
      "[grok-companion] implement: no runId in the code envelope; cannot hand off.\n"
    );
    // Normalize any failure (including raw spawn exit codes) to 1.
    return 1;
  }
  stderrLine(`[grok-implement] code finished (exit ${code}); verifying handoff for ${runId}`);
  const { code: hCode, envelope: hEnv } = runHandoffCaptured(wrapper, [
    "handoff",
    "--run-id",
    runId,
  ]);
  // Real handoff success shape (modes/handoff.py): response.integration.ready
  const ready = hEnv?.response?.integration?.ready === true;
  stderrLine(`[grok-implement] handoff ${ready ? "READY" : "NOT READY"} for ${runId}`);
  return code === 0 && hCode === 0 && ready ? 0 : 1;
}
