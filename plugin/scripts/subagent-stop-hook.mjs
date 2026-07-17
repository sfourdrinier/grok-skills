#!/usr/bin/env node
// plugin/scripts/subagent-stop-hook.mjs
//
// SubagentStop handoff nudge (non-blocking, read-only).
//
// Host contract (Claude Code hooks, July 2026):
//   stdin JSON fields include: last_assistant_message, agent_id, agent_type
//   (plugin-scoped, e.g. "grok:grok-engineer-coder"), session_id,
//   transcript_path, cwd, hook_event_name.
//   stdout JSON {"hookSpecificOutput": {"additionalContext": "..."}} with
//   exit 0 adds context without blocking. Exit 2 would block - never used here.
//
// Behavior: when agent_type is grok-engineer-coder (exact plugin-scoped form
// or any string ending in ":grok-engineer-coder"), scan listJobs(cwd) for the
// newest kind "code" job whose runId has an existing run dir and no
// handoff-consumed.json marker; emit a dual-condition handoff reminder.
// Garbage input / no match / any error: silent exit 0. Does not write markers.

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { listJobs } from "./lib/jobs.mjs";
import { readAllStdinSync } from "./lib/read-stdin.mjs";
import { runsDirFor, safeRunIdForRunsDir } from "./progress-relay.mjs";

const HANDOFF_MARKER = "handoff-consumed.json";
const ENGINEER_SUFFIX = ":grok-engineer-coder";
const ENGINEER_SCOPED = "grok:grok-engineer-coder";

/**
 * @param {unknown} agentType
 * @returns {boolean}
 */
export function isGrokEngineerCoder(agentType) {
  if (typeof agentType !== "string" || !agentType) return false;
  return agentType === ENGINEER_SCOPED || agentType.endsWith(ENGINEER_SUFFIX);
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
 * @param {string} runId
 * @returns {string}
 */
export function handoffReminderContext(runId) {
  return (
    `Grok code run ${runId} finished. Before integrating, run handoff --run-id ${runId} ` +
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

  if (!isGrokEngineerCoder(input.agent_type)) {
    process.exit(0);
  }

  const cwd =
    typeof input.cwd === "string" && input.cwd.trim()
      ? input.cwd
      : process.env.CLAUDE_PROJECT_DIR || process.cwd();

  let runId;
  try {
    runId = findNewestUnconsumedCodeRunId(cwd, process.env);
  } catch {
    process.exit(0);
  }

  if (!runId) {
    process.exit(0);
  }

  process.stdout.write(
    `${JSON.stringify({
      hookSpecificOutput: {
        additionalContext: handoffReminderContext(runId),
      },
    })}\n`
  );
  process.exit(0);
}

try {
  main();
} catch {
  // Never block SubagentStop: any unexpected failure is silent exit 0.
  process.exit(0);
}
