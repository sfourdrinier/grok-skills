// plugin/scripts/lib/implement.mjs
//
// One-call implement combo: code (live relay) then handoff verification.
// Shared runCodeThenHandoff is also used by integration=auto (apply step in
// integrate.mjs). Exit 0 for implement only when code succeeded AND handoff is
// dual-condition ready. Handoff still runs after failed code when a runId
// exists (surface blockers). Direct-mode refusal reuses DIRECT_NO_HANDOFF_MSG.

import { spawnSync } from "node:child_process";

import { sanitizeRunId } from "./companion-terminal-notify.mjs";
import { DIRECT_NO_HANDOFF_MSG, writeDirectNoHandoffRefuse } from "./direct-grok.mjs";
import { parseTargetFlag, resolveTargetWorkspaceRoot } from "./git-context.mjs";
import { applyVerifiedPatch } from "./integrate.mjs";
import { withExplicitIntegration } from "./jobs.mjs";
import { wrapperChildEnv } from "./notify.mjs";
import { tryParseEnvelope } from "./render.mjs";

export { DIRECT_NO_HANDOFF_MSG };

/**
 * Map companion integration modes to wrapper --integration values.
 * Wrapper only accepts direct|worktree; auto/review run as isolated worktree.
 *
 * @param {string|null|undefined} mode
 * @returns {"direct"|"worktree"}
 */
export function companionIntegrationToWrapper(mode) {
  if (mode === "direct") return "direct";
  return "worktree";
}

/**
 * Parse the last --integration value from argv (supports --integration=).
 * @param {string[]} args
 * @returns {string|null}
 */
function parseIntegrationFromArgs(args) {
  if (!Array.isArray(args)) return null;
  let found = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--integration" && args[i + 1] !== undefined) {
      found = String(args[i + 1]);
      i += 1;
      continue;
    }
    if (typeof a === "string" && a.startsWith("--integration=")) {
      found = a.slice("--integration=".length);
    }
  }
  return found;
}

/**
 * Rewrite rest so the wrapper receives a supported --integration value.
 * auto|review -> worktree; direct stays direct.
 *
 * @param {string[]} rest
 * @returns {string[]}
 */
export function restForWrapperIntegration(rest) {
  const mode = parseIntegrationFromArgs(rest);
  const wrapperMode = companionIntegrationToWrapper(mode);
  return withExplicitIntegration(Array.isArray(rest) ? rest : [], wrapperMode);
}

/**
 * Capture handoff stdout so implement/auto can read response.integration.ready.
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
 * Shared helper: run code (worktree-mapped integration) then handoff.
 * Used by implement (ready-gated exit) and auto (then applyVerifiedPatch).
 *
 * @returns {Promise<{
 *   codeExit: number,
 *   codeEnvelope: object|null,
 *   handoffCode: number|null,
 *   handoffEnvelope: object|null,
 *   ready: boolean,
 *   runId: string|null,
 * }>}
 */
export async function runCodeThenHandoff(wrapper, rest, track, {
  runWithLiveRelay,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
  logPrefix = "implement",
} = {}) {
  if (typeof runWithLiveRelay !== "function") {
    process.stderr.write(
      `[grok-companion] ${logPrefix}: runWithLiveRelay is required\n`
    );
    return {
      codeExit: 1,
      codeEnvelope: null,
      handoffCode: null,
      handoffEnvelope: null,
      ready: false,
      runId: null,
    };
  }
  const wrapperRest = restForWrapperIntegration(rest);
  const codeArgs = ["code", ...wrapperRest];
  const res = await runWithLiveRelay(wrapper, codeArgs, {
    ...track,
    captureStdout: true,
  });
  const codeExit = typeof res === "number" ? res : res.code;
  const stdoutBuf = typeof res === "number" ? "" : res.stdout || "";
  const codeEnvelope = tryParseEnvelope(stdoutBuf);
  const runId = sanitizeRunId(codeEnvelope?.runId);
  if (!runId) {
    process.stderr.write(
      `[grok-companion] ${logPrefix}: no runId in the code envelope; cannot hand off.\n`
    );
    return {
      codeExit,
      codeEnvelope,
      handoffCode: null,
      handoffEnvelope: null,
      ready: false,
      runId: null,
    };
  }
  stderrLine(
    `[grok-${logPrefix}] code finished (exit ${codeExit}); verifying handoff for ${runId}`
  );
  const { code: handoffCode, envelope: handoffEnvelope } = runHandoffCaptured(
    wrapper,
    ["handoff", "--run-id", runId]
  );
  const ready = handoffEnvelope?.response?.integration?.ready === true;
  stderrLine(
    `[grok-${logPrefix}] handoff ${ready ? "READY" : "NOT READY"} for ${runId}`
  );
  return {
    codeExit,
    codeEnvelope,
    handoffCode,
    handoffEnvelope,
    ready,
    runId,
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
  const result = await runCodeThenHandoff(wrapper, rest, track, {
    runWithLiveRelay,
    stderrLine,
    logPrefix: "implement",
  });
  if (!result.runId) return 1;
  return result.codeExit === 0 && result.handoffCode === 0 && result.ready
    ? 0
    : 1;
}

/**
 * integration=auto: code in isolated worktree + handoff, then apply-on-verified-ready
 * with apply-time revalidation (lib/integrate.mjs). Exit 0 only when code ok,
 * handoff ready, and apply succeeded.
 *
 * @param {string} wrapper
 * @param {string[]} rest
 * @param {string} runMode
 * @param {object} track
 * @param {object} [opts]
 * @param {Function} opts.runWithLiveRelay
 * @param {(line: string) => void} [opts.stderrLine]
 * @param {string} [opts.targetCwd] companion cwd for --target resolution
 * @returns {Promise<number>}
 */
export async function runAutoIntegrate(wrapper, rest, runMode, track, {
  runWithLiveRelay,
  stderrLine = (line) => process.stderr.write(`${line}\n`),
  targetCwd = process.cwd(),
} = {}) {
  if (runMode === "direct") {
    return writeDirectNoHandoffRefuse();
  }
  const result = await runCodeThenHandoff(wrapper, rest, track, {
    runWithLiveRelay,
    stderrLine,
    logPrefix: "auto",
  });
  if (!result.runId) return 1;
  if (!(result.codeExit === 0 && result.handoffCode === 0 && result.ready)) {
    stderrLine(
      `[grok-auto] not applying: code/handoff not dual-condition ready for ${result.runId}`
    );
    return 1;
  }
  const targetArg = parseTargetFlag(rest);
  const targetRepo = resolveTargetWorkspaceRoot(targetCwd, targetArg);
  stderrLine(`[grok-auto] ready; applying patch to target ${targetRepo}`);
  const applied = applyVerifiedPatch({
    wrapper,
    runId: result.runId,
    targetRepo,
    runHandoff: runHandoffCaptured,
    stderrLine,
  });
  return applied.ok ? 0 : 1;
}
