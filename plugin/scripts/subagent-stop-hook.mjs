#!/usr/bin/env node
// plugin/scripts/subagent-stop-hook.mjs
//
// SubagentStop mode-aware nudge (non-blocking, read-only).
//
// Host contract (Claude Code hooks, July 2026):
//   stdin JSON fields include: last_assistant_message, agent_id, agent_type
//   (plugin-scoped, e.g. "grok:grok-engineer-coder", or bare
//   "grok-engineer-coder"), session_id, transcript_path, cwd, hook_event_name.
//   stdout JSON {"hookSpecificOutput": {"additionalContext": "..."}} with
//   exit 0 adds context without blocking. Exit 2 would block - never used here.
//
// agent_type and last_assistant_message are host-supplied. This hook only ever
// emits advisory context with shape-validated runIds (safeRunIdForRunsDir);
// it never trusts raw message text as a path component.
//
// Behavior: when agent_type is grok-engineer-coder (exact plugin-scoped form,
// bare name, or any string ending in ":grok-engineer-coder"):
//   1. Prefer a runId from last_assistant_message: scan ALL RUN_ID_RE-shaped
//      tokens; use the LAST one whose run dir exists under runs/.
//   2. Else fall back to newest kind "code" job with an unconsumed run dir,
//      and soften the reminder text to "most recent code run in this workspace".
//   3. Mode-aware text:
//      - durable peer (peer.json or peer job kind/mode) -> peer-stop outcome
//        (never handoff, never apply)
//      - durable code job kind -> handoff --run-id + dual-condition ready
//        (never auto-apply); peer-stop wording alone cannot override this
//      - peer wording without durable code evidence may classify as peer
// Garbage input / no match / any error: silent exit 0. Does not write markers.

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

import { listJobs } from "./lib/jobs.mjs";
import { readAllStdinSync } from "./lib/read-stdin.mjs";
import { RUN_ID_RE, runsDirFor, safeRunIdForRunsDir } from "./progress-relay.mjs";

const HANDOFF_MARKER = "handoff-consumed.json";
const ENGINEER_SUFFIX = ":grok-engineer-coder";
const ENGINEER_SCOPED = "grok:grok-engineer-coder";
const ENGINEER_BARE = "grok-engineer-coder";

// Global scan for RUN_ID_RE-shaped tokens inside free text (strip ^/$ anchors).
const RUN_ID_TOKEN_RE = new RegExp(
  RUN_ID_RE.source.replace(/^\^/, "").replace(/\$$/, ""),
  "g"
);

/**
 * @param {unknown} agentType
 * @returns {boolean}
 */
export function isGrokEngineerCoder(agentType) {
  if (typeof agentType !== "string" || !agentType) return false;
  return (
    agentType === ENGINEER_SCOPED ||
    agentType === ENGINEER_BARE ||
    agentType.endsWith(ENGINEER_SUFFIX)
  );
}

/**
 * True when free text clearly describes a peer-channel run (not one-shot code).
 * Used to mode-switch the engineer-coder nudge away from handoff.
 * @param {unknown} message
 * @returns {boolean}
 */
export function messageLooksLikePeerRun(message) {
  if (typeof message !== "string" || !message) return false;
  // peer stop / peer-stop / peer start / mode peer-stop / "peer stop --run-id"
  return /\bpeer[\s_-]*(start|prompt|stop)\b/i.test(message) || /\bpeer-stop\b/i.test(message);
}

/**
 * True when the durable run dir is a peer channel run (peer.json present) or
 * a workspace job for that runId is kind/mode peer*.
 * @param {string} runId
 * @param {string} cwd
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {boolean}
 */
export function isPeerRunId(runId, cwd, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe) return false;
  try {
    if (fs.existsSync(path.join(runsDir, safe, "peer.json"))) return true;
  } catch {
    // ignore
  }
  try {
    const jobs = listJobs(cwd, env);
    for (const job of jobs) {
      if (!job || job.runId !== safe) continue;
      const kind = String(job.kind || "");
      const mode = String(job.mode || "");
      if (kind.startsWith("peer") || mode.startsWith("peer")) return true;
    }
  } catch {
    // ignore
  }
  return false;
}

/**
 * True when a workspace job for that runId is durable kind "code".
 * Used so peer-stop wording cannot override a real code handoff nudge.
 * @param {string} runId
 * @param {string} cwd
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {boolean}
 */
export function isCodeRunId(runId, cwd, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe) return false;
  try {
    const jobs = listJobs(cwd, env);
    for (const job of jobs) {
      if (!job || job.runId !== safe) continue;
      if (String(job.kind || "") === "code") return true;
    }
  } catch {
    // ignore
  }
  return false;
}

/**
 * Mode-aware SubagentStop advisory text.
 * - code: handoff --run-id + dual-condition ready (never auto-apply)
 * - peer: refer to peer-stop outcome (never handoff, never apply)
 * @param {string} runId
 * @param {{ fromMessage?: boolean, peer?: boolean }} [opts]
 * @returns {string}
 */
export function reminderContextForAgent(runId, opts = {}) {
  if (opts.peer) {
    return (
      `Grok peer run ${runId} finished. Use the peer-stop outcome as the ready signal ` +
      "(do not run handoff; peer-stop owns ready + integrate). Never auto-apply."
    );
  }
  return handoffReminderContext(runId, { fromMessage: opts.fromMessage });
}

/**
 * Scan last_assistant_message for RUN_ID_RE tokens; return the LAST one that
 * has an existing run dir under runs/ (shape-validated via safeRunIdForRunsDir).
 *
 * @param {unknown} message
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null}
 */
export function findValidatedRunIdFromMessage(message, env = process.env) {
  if (typeof message !== "string" || !message) return null;
  const runsDir = runsDirFor(env);
  const tokens = message.match(RUN_ID_TOKEN_RE);
  if (!tokens || tokens.length === 0) return null;
  // Prefer the last shape-valid token whose run dir exists.
  for (let i = tokens.length - 1; i >= 0; i--) {
    const runId = safeRunIdForRunsDir(tokens[i], runsDir);
    if (!runId) continue;
    const runDir = path.join(runsDir, runId);
    let st;
    try {
      st = fs.statSync(runDir);
    } catch {
      continue;
    }
    if (st.isDirectory()) {
      return runId;
    }
  }
  return null;
}

/**
 * Newest-first listJobs order; first kind "code" with runId whose run dir
 * exists and lacks handoff-consumed.json wins.
 *
 * @param {string} cwd
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string|null} runId
 */
export function findNewestUnconsumedCodeRunId(cwd, env = process.env) {
  const runsDir = runsDirFor(env);
  const jobs = listJobs(cwd, env);
  for (const job of jobs) {
    if (!job || job.kind !== "code") continue;
    const runId = safeRunIdForRunsDir(job.runId, runsDir);
    if (!runId) continue;
    const runDir = path.join(runsDir, runId);
    let st;
    try {
      st = fs.statSync(runDir);
    } catch {
      continue;
    }
    if (!st.isDirectory()) continue;
    if (fs.existsSync(path.join(runDir, HANDOFF_MARKER))) continue;
    return runId;
  }
  return null;
}

/**
 * Mark a code run's handoff as consumed so the SubagentStop fallback stops
 * re-suggesting it (the marker previously had no production writer, so every
 * checked run stayed "unconsumed" forever). Called from the companion after a
 * ready `/grok:handoff`. Best-effort: never throws.
 * @param {string} runId
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {boolean} true when the marker was written
 */
export function writeHandoffConsumedMarker(runId, env = process.env) {
  const runsDir = runsDirFor(env);
  const safe = safeRunIdForRunsDir(runId, runsDir);
  if (!safe) return false;
  const runDir = path.join(runsDir, safe);
  try {
    if (!fs.statSync(runDir).isDirectory()) return false;
    fs.writeFileSync(
      path.join(runDir, HANDOFF_MARKER),
      JSON.stringify({ handoffRunId: safe }) + "\n"
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * @param {string} runId
 * @param {{ fromMessage?: boolean }} [opts]
 * @returns {string}
 */
export function handoffReminderContext(runId, opts = {}) {
  if (opts.fromMessage) {
    return (
      `Grok code run ${runId} finished. Before integrating, run handoff --run-id ${runId} ` +
      "and require dual-condition ready (never auto-apply)."
    );
  }
  return (
    `Grok code run finished (most recent code run in this workspace: ${runId}). ` +
    `Before integrating, run handoff --run-id ${runId} ` +
    "and require dual-condition ready (never auto-apply)."
  );
}

function readHookInput() {
  try {
    const raw = readAllStdinSync().toString("utf8").trim();
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function main() {
  const input = readHookInput();
  if (!input) {
    process.exit(0);
  }

  // Only engineer-coder (the dual peer/code implementer) receives this nudge.
  if (!isGrokEngineerCoder(input.agent_type)) {
    process.exit(0);
  }

  const cwd =
    typeof input.cwd === "string" && input.cwd.trim()
      ? input.cwd
      : process.env.CLAUDE_PROJECT_DIR || process.cwd();

  let runId = null;
  let fromMessage = false;
  try {
    const fromMsg = findValidatedRunIdFromMessage(input.last_assistant_message, process.env);
    if (fromMsg) {
      runId = fromMsg;
      fromMessage = true;
    } else {
      runId = findNewestUnconsumedCodeRunId(cwd, process.env);
    }
  } catch {
    process.exit(0);
  }

  if (!runId) {
    process.exit(0);
  }

  // Mode-aware: peer runs must NEVER be told to handoff/auto-apply.
  // Peer classification requires durable peer.json or peer job kind.
  // Peer-stop wording alone may classify as peer ONLY when there is no durable
  // code job evidence for this runId (wording cannot override a real code run).
  const durablePeer = isPeerRunId(runId, cwd, process.env);
  const durableCode = isCodeRunId(runId, cwd, process.env);
  const peer =
    durablePeer ||
    (!durableCode &&
      fromMessage &&
      messageLooksLikePeerRun(input.last_assistant_message));

  const context = reminderContextForAgent(runId, { fromMessage, peer });

  process.stdout.write(
    `${JSON.stringify({
      hookSpecificOutput: {
        additionalContext: context,
      },
    })}\n`
  );
  process.exit(0);
}

// Only run the hook when executed as a script; importing (e.g. the companion
// reusing writeHandoffConsumedMarker) must not read stdin or exit.
if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  try {
    main();
  } catch {
    // Never block SubagentStop: any unexpected failure is silent exit 0.
    process.exit(0);
  }
}
