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
  // silent: capture the envelope WITHOUT relaying stdout. Apply-time revalidation
  // (auto) reuses this only to re-check readiness; relaying it would emit a second
  // JSON object after the handoff envelope already went to stdout, breaking the
  // one-stdout-envelope-per-run contract that the code skill documents.
  silent = false,
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
  if (stdout && !silent) process.stdout.write(stdout.endsWith("\n") ? stdout : `${stdout}\n`);
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
  // combine: suppress the intermediate code + handoff stdout relays so the caller
  // (auto) can emit exactly ONE final envelope. implement keeps combine=false and
  // its documented two-envelope (code then handoff) relay.
  combine = false,
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
  // Suppress the code-leg's terminal notification: the combo's real outcome is
  // only known after handoff (implement) / apply (auto), so the caller fires one
  // notification then via finalizeCombo. captureStdout also returns the jobId so
  // the caller can re-finalize the job status to the true outcome.
  const res = await runWithLiveRelay(wrapper, codeArgs, {
    ...track,
    captureStdout: true,
    skipNotify: true,
    suppressStdoutRelay: combine,
  });
  const codeExit = typeof res === "number" ? res : res.code;
  const stdoutBuf = typeof res === "number" ? "" : res.stdout || "";
  const jobId = typeof res === "number" ? null : res.jobId ?? null;
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
      jobId,
      codeStdout: stdoutBuf,
    };
  }
  stderrLine(
    `[grok-${logPrefix}] code finished (exit ${codeExit}); verifying handoff for ${runId}`
  );
  const { code: handoffCode, envelope: handoffEnvelope } = runHandoffCaptured(
    wrapper,
    ["handoff", "--run-id", runId],
    { silent: combine }
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
    jobId,
    codeStdout: stdoutBuf,
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
  finalizeCombo = null,
} = {}) {
  if (runMode === "direct") {
    return writeDirectNoHandoffRefuse();
  }
  const result = await runCodeThenHandoff(wrapper, rest, track, {
    runWithLiveRelay,
    stderrLine,
    logPrefix: "implement",
  });
  const finalCode = !result.runId
    ? 1
    : result.codeExit === 0 && result.handoffCode === 0 && result.ready
      ? 0
      : 1;
  // implement's LIVE stdout stays two envelopes (code then handoff, documented),
  // but the STORED stdout.json becomes the handoff envelope so /grok:result shows
  // the true readiness/blockers, not a stale SUCCESS code envelope.
  // Missing/null handoff reuses the auto fallback SSOT so store never keeps the
  // code-leg success payload when handoff is unavailable.
  let finalEnvelopeText;
  if (result.handoffEnvelope && typeof result.handoffEnvelope === "object") {
    finalEnvelopeText = `${JSON.stringify(result.handoffEnvelope)}\n`;
  } else if (finalCode !== 0) {
    finalEnvelopeText = `${JSON.stringify(buildAutoCodeFallbackEnvelope(result))}\n`;
  } else {
    finalEnvelopeText = "";
  }
  // Finalize the job status + fire ONE notification on the true outcome (the
  // code-leg notify was suppressed), so /grok:jobs and notifications never
  // report success for a not-ready implement.
  if (typeof finalizeCombo === "function") {
    await finalizeCombo({
      jobId: result.jobId,
      finalCode,
      runId: result.runId,
      stdoutText: result.codeStdout,
      finalEnvelopeText,
    });
  }
  return finalCode;
}

/**
 * SSOT: attach final integration outcome onto a base envelope (auto handoff or
 * peer-stop wrapper). Sets TRUE terminal status + response.integration.applied
 * / outcome so stdout, /grok:result storage, and notify share one shape.
 *
 * @param {object|null|undefined} baseEnvelope
 * @param {number} finalCode
 * @param {{ok?: boolean, outcome?: string}|null} applied
 * @param {{
 *   mode?: string,
 *   readyFallback?: boolean,
 *   forceReady?: boolean|null,
 * }} [opts]
 *   forceReady: when boolean, overwrite response.integration.ready (auto failure
 *   fallback forces false so a code-leg ready claim cannot leak into the final).
 * @returns {object|null}
 */
export function attachIntegrationFinalOutcome(baseEnvelope, finalCode, applied, opts = {}) {
  if (!baseEnvelope || typeof baseEnvelope !== "object") return null;
  const baseResp =
    baseEnvelope.response && typeof baseEnvelope.response === "object"
      ? baseEnvelope.response
      : {};
  const baseInteg =
    baseResp.integration && typeof baseResp.integration === "object"
      ? baseResp.integration
      : {};
  const integration = {
    ...baseInteg,
    applied: applied?.ok === true,
    outcome:
      applied?.outcome ?? (opts.readyFallback === true ? "not-applied" : "not-ready"),
  };
  if (typeof opts.forceReady === "boolean") {
    integration.ready = opts.forceReady;
  }
  const response = {
    ...baseResp,
    integration,
  };
  // When requested (peer final / onStdout failure SSOT), clear peer.integrationReady
  // on failure so a blocked apply never looks ready downstream.
  if (
    finalCode !== 0 &&
    (opts.clearPeerReady === true || opts.forceReady === false)
  ) {
    if (baseResp.peer && typeof baseResp.peer === "object") {
      response.peer = { ...baseResp.peer, integrationReady: false };
    } else if (opts.clearPeerReady === true) {
      response.peer = { integrationReady: false };
    }
  }
  const out = {
    ...baseEnvelope,
    status: finalCode === 0 ? "success" : "failure",
    response,
  };
  if (typeof opts.mode === "string" && opts.mode) {
    out.mode = opts.mode;
  }
  if (opts.error && typeof opts.error === "object") {
    out.error = opts.error;
  }
  return out;
}

/**
 * SSOT failure envelope for peer-stop / onStdout integration errors: one complete
 * envelope with applied=false, ready signals cleared, nonzero-path status=failure.
 * Used by companion-capture onStdout throws and any peer final rewrite that must
 * not leave raw ready success on stdout/store/notify.
 *
 * @param {object|null|undefined} rawEnvelope
 * @param {{class?: string, message?: string, detail?: object}|string|Error} error
 * @param {{mode?: string, outcome?: string}} [opts]
 * @returns {object}
 */
export function buildPeerIntegrationFailureEnvelope(rawEnvelope, error, opts = {}) {
  const errObj =
    error && typeof error === "object" && typeof error.class === "string"
      ? error
      : {
          class: "integration-error",
          message:
            typeof error === "string"
              ? error
              : error && typeof error.message === "string"
                ? error.message
                : "integration error",
        };
  const base =
    rawEnvelope && typeof rawEnvelope === "object"
      ? { ...rawEnvelope }
      : {
          schemaVersion: 1,
          mode: opts.mode || "peer-stop",
          status: "failure",
          runId: null,
          response: null,
        };
  base.schemaVersion = typeof base.schemaVersion === "number" ? base.schemaVersion : 1;
  if (typeof opts.mode === "string" && opts.mode) base.mode = opts.mode;
  const attached = attachIntegrationFinalOutcome(
    base,
    1,
    { ok: false, outcome: opts.outcome || "integration-error" },
    {
      forceReady: false,
      clearPeerReady: true,
      error: errObj,
      ...(typeof opts.mode === "string" && opts.mode ? { mode: opts.mode } : {}),
    }
  );
  return attached || { ...base, status: "failure", error: errObj };
}

/**
 * Build the single final auto envelope from the handoff envelope (which carries
 * runId + response.integration.ready + blockers), setting the TRUE combo status
 * and recording whether the ready patch actually applied. Returns null when there
 * is no handoff envelope (no runId) so the caller falls back to the code stdout.
 * @param {object} result runCodeThenHandoff result
 * @param {number} finalCode
 * @param {{ok: boolean, outcome: string}|null} applied applyVerifiedPatch result
 * @returns {object|null}
 */
export function buildAutoFinalEnvelope(result, finalCode, applied) {
  // Terminal envelope for a `code --integration auto` COMMAND keys as mode "code"
  // (callers dispatch on envelope.mode; not the handoff we built it from).
  return attachIntegrationFinalOutcome(result?.handoffEnvelope, finalCode, applied, {
    mode: "code",
    readyFallback: result?.ready === true,
  });
}

/**
 * SSOT fallback when auto has no handoff envelope: preserve a parseable code
 * envelope when present, else synthesize a COMPLETE one-stdout failure envelope
 * (schemaVersion/mode/status/runId/error/response.integration). Never invent
 * success; never invent a runId (known result/job run id or explicit null).
 *
 * Classification (existing C4 classes only):
 * - preserve code-leg error.class when present
 * - parseable code envelope without a usable runId -> handoff-unavailable
 *   (cannot hand off / apply; not stdout corruption)
 * - non-empty unparseable stdout -> output-malformed
 * - empty stdout -> output-missing
 *
 * Integration fields route through attachIntegrationFinalOutcome (ready forced
 * false; applied false; outcome not-ready).
 *
 * @param {{
 *   codeEnvelope?: object|null,
 *   runId?: string|null,
 *   jobId?: string|null,
 *   codeExit?: number|null,
 *   codeStdout?: string|null,
 * }} result
 * @returns {object}
 */
export function buildAutoCodeFallbackEnvelope(result = {}) {
  const ce =
    result.codeEnvelope && typeof result.codeEnvelope === "object" ? result.codeEnvelope : null;
  const knownRunId =
    sanitizeRunId(result.runId) ||
    sanitizeRunId(ce?.runId) ||
    null;
  const preservedError =
    ce?.error && typeof ce.error === "object" && typeof ce.error.class === "string"
      ? ce.error
      : null;
  const stdout = typeof result.codeStdout === "string" ? result.codeStdout : "";
  const exitCode = typeof result.codeExit === "number" ? result.codeExit : null;
  const detailBase = {
    exitCode,
    jobId: result.jobId ?? null,
    stdoutBytes: Buffer.byteLength(stdout, "utf8"),
  };
  let error = preservedError;
  if (!error) {
    if (ce) {
      // Parseable code envelope is never stdout corruption. Without a usable
      // runId (or without a handoff envelope on this path) classify
      // handoff-unavailable so auto cannot claim apply success.
      error = {
        class: "handoff-unavailable",
        message: knownRunId
          ? "code envelope is parseable but no handoff envelope is available for auto apply"
          : "code envelope is parseable but has no usable runId; cannot hand off or auto-apply",
        detail: detailBase,
      };
    } else if (stdout.trim()) {
      error = {
        class: "output-malformed",
        message:
          "code leg produced no parseable envelope (stdout corruption or non-JSON wrapper output)",
        detail: detailBase,
      };
    } else {
      error = {
        class: "output-missing",
        message:
          "code leg produced no parseable envelope (empty stdout; spawn failure or wrapper crash)",
        detail: { ...detailBase, stdoutBytes: 0 },
      };
    }
  }
  const base = ce
    ? { ...ce }
    : {
        schemaVersion: 1,
        mode: "code",
        status: "failure",
        runId: knownRunId,
        error,
        response: null,
      };
  // Force honest failure even when the code envelope claimed success without a
  // usable runId (auto never applied in this branch).
  base.schemaVersion = typeof base.schemaVersion === "number" ? base.schemaVersion : 1;
  base.mode = "code";
  base.status = "failure";
  base.runId = knownRunId;
  if (!base.error || typeof base.error !== "object" || typeof base.error.class !== "string") {
    base.error = error;
  }
  // Route integration outcome fields through the shared SSOT (no parallel field
  // construction). forceReady=false so a code-leg ready claim cannot leak.
  const attached = attachIntegrationFinalOutcome(
    base,
    1,
    { ok: false, outcome: "not-ready" },
    { mode: "code", readyFallback: false, forceReady: false }
  );
  return attached || base;
}

/**
 * Peer-stop terminal envelope: rewrite the wrapper's ready/success envelope with
 * the real apply outcome BEFORE first stdout write / store / notify.
 * applied is true only for an attempted+ok apply (retained/not-ready stay false).
 *
 * @param {object|null|undefined} rawEnvelope wrapper peer-stop envelope
 * @param {number} finalCode
 * @param {{attempted?: boolean, ok?: boolean, outcome?: string}|null} peerIntegration
 * @returns {object|null}
 */
export function buildPeerStopFinalEnvelope(rawEnvelope, finalCode, peerIntegration) {
  const applied =
    peerIntegration == null
      ? null
      : {
          ok: peerIntegration.attempted === true && peerIntegration.ok === true,
          outcome: peerIntegration.outcome,
        };
  const ready =
    rawEnvelope?.response?.peer?.integrationReady === true ||
    rawEnvelope?.response?.integration?.ready === true;
  return attachIntegrationFinalOutcome(rawEnvelope, finalCode, applied, {
    readyFallback: ready === true,
    // Failures clear ready signals (including peer.integrationReady) via SSOT.
    clearPeerReady: finalCode !== 0,
    forceReady: finalCode !== 0 ? false : undefined,
  });
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
  finalizeCombo = null,
} = {}) {
  if (runMode === "direct") {
    return writeDirectNoHandoffRefuse();
  }
  // combine:true suppresses the code + initial-handoff stdout relays; auto emits
  // exactly ONE final envelope below (the `code` single-envelope contract).
  const result = await runCodeThenHandoff(wrapper, rest, track, {
    runWithLiveRelay,
    stderrLine,
    logPrefix: "auto",
    combine: true,
  });
  let finalCode;
  let applied = null;
  if (!result.runId) {
    finalCode = 1;
  } else if (!(result.codeExit === 0 && result.handoffCode === 0 && result.ready)) {
    stderrLine(
      `[grok-auto] not applying: code/handoff not dual-condition ready for ${result.runId}`
    );
    finalCode = 1;
  } else {
    const targetArg = parseTargetFlag(rest);
    const targetRepo = resolveTargetWorkspaceRoot(targetCwd, targetArg);
    stderrLine(`[grok-auto] ready; applying patch to target ${targetRepo}`);
    applied = applyVerifiedPatch({
      wrapper,
      runId: result.runId,
      targetRepo,
      // Silent capture: the apply-time revalidation must not emit a second envelope.
      runHandoff: (w, a) => runHandoffCaptured(w, a, { silent: true }),
      stderrLine,
    });
    finalCode = applied.ok ? 0 : 1;
  }
  // Emit + store exactly one final outcome envelope (handoff envelope carries the
  // runId + integration.ready + blockers; we set the true combo status and record
  // the apply outcome). Falls back to the captured code envelope when there was no
  // runId (nothing to hand off).
  const finalEnvelope = buildAutoFinalEnvelope(result, finalCode, applied);
  let finalEnvelopeText;
  if (finalEnvelope) {
    finalEnvelopeText = `${JSON.stringify(finalEnvelope)}\n`;
  } else {
    // No handoff envelope (no runId / handoff crashed or returned non-JSON): emit
    // a COMPLETE one-stdout failure envelope (preserve code envelope when present;
    // otherwise synthesize classified error + integration applied=false). Never
    // invent success or hide the original spawn/parse failure.
    finalEnvelopeText = `${JSON.stringify(buildAutoCodeFallbackEnvelope(result))}\n`;
  }
  if (finalEnvelopeText) {
    process.stdout.write(
      finalEnvelopeText.endsWith("\n") ? finalEnvelopeText : `${finalEnvelopeText}\n`
    );
  }
  if (typeof finalizeCombo === "function") {
    await finalizeCombo({
      jobId: result.jobId,
      finalCode,
      runId: result.runId,
      stdoutText: result.codeStdout,
      finalEnvelopeText,
    });
  }
  return finalCode;
}
