#!/usr/bin/env node
// plugin/scripts/stop-review-gate-hook.mjs
//
// Optional Stop-time review gate. Opt-in per workspace via /grok:setup.
// Always forces hardened mode + --no-web + review schema for machine findings.
// Fail closed on free-text success, spawn/auth failures, and unreadable config
// when the gate appears enabled.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { classifyReviewRun } from "./lib/gate-decision.mjs";
import { resolveSpawnedGroupPid, terminateReviewTree } from "./lib/gate-kill.mjs";
import { readGateConfig } from "./lib/gate-state.mjs";
import { getRunMode } from "./lib/jobs.mjs";
import { readAllStdinSync } from "./lib/read-stdin.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.join(SCRIPT_DIR, "grok-companion.mjs");
const REVIEW_SCHEMA = path.join(SCRIPT_DIR, "..", "schemas", "review-output.schema.json");
const STOP_REVIEW_TIMEOUT_MS = 840 * 1000;
const STOP_REVIEW_MAX_BUFFER_BYTES = 64 * 1024 * 1024;

function resolveStopReviewTimeoutMs() {
  const raw = (process.env.GROK_STOP_REVIEW_TIMEOUT_MS ?? "").trim();
  if (raw) {
    const parsed = Number(raw);
    if (Number.isInteger(parsed) && parsed > 0) {
      return Math.min(parsed, STOP_REVIEW_TIMEOUT_MS);
    }
    process.stderr.write(`[grok-stop-gate] ignoring invalid GROK_STOP_REVIEW_TIMEOUT_MS=${raw}\n`);
  }
  return STOP_REVIEW_TIMEOUT_MS;
}

function readHookInput() {
  try {
    const raw = readAllStdinSync().toString("utf8").trim();
    return raw ? JSON.parse(raw) : {};
  } catch (err) {
    process.stderr.write(`[grok-stop-gate] could not read hook input: ${err.message}\n`);
    return {};
  }
}

function emitDecision(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function emitAllow() {
  emitDecision({ continue: true });
}

function buildStopReviewTask(input) {
  const lastAssistantMessage = String(input.last_assistant_message ?? "").trim();
  const lines = [
    "Stop-gate review of the previous assistant turn.",
    "Read the workspace rules and the changed code, and review the previous turn for correctness bugs, safety-rule violations, and incomplete work.",
    "Emit structured findings with severity. Do not edit any files.",
  ];
  if (lastAssistantMessage) {
    lines.push("", "Previous assistant response:", lastAssistantMessage);
  }
  return lines.join("\n");
}

function runStopReview(cwd, input) {
  const taskText = buildStopReviewTask(input);
  const args = [COMPANION, "review", "--target", ".", "--no-web", "--task-file", "-"];
  if (fs.existsSync(REVIEW_SCHEMA)) {
    args.push("--schema", REVIEW_SCHEMA);
  } else {
    process.stderr.write(
      `[grok-stop-gate] review schema missing at ${REVIEW_SCHEMA}; gate will fail closed without structured findings\n`
    );
  }

  // Force hardened: never run the stop gate under direct mode.
  const env = { ...process.env, GROK_SKILLS_MODE: "hardened" };
  const isPosix = process.platform !== "win32";
  const result = spawnSync(process.execPath, args, {
    cwd,
    env,
    encoding: "utf8",
    timeout: resolveStopReviewTimeoutMs(),
    maxBuffer: STOP_REVIEW_MAX_BUFFER_BYTES,
    input: taskText,
    detached: isPosix,
    killSignal: "SIGTERM",
    stdio: ["pipe", "pipe", "inherit"],
  });

  const spawnedPid = resolveSpawnedGroupPid(result);
  const timedOut =
    Boolean(result.error && (result.error.code === "ETIMEDOUT" || /ETIMEDOUT/i.test(String(result.error)))) ||
    result.signal === "SIGTERM" ||
    result.signal === "SIGKILL";
  if (spawnedPid !== null && (result.signal || result.error || timedOut)) {
    terminateReviewTree(spawnedPid, isPosix);
  }

  return classifyReviewRun(result);
}

function main() {
  const input = readHookInput();

  if (input.stop_hook_active) {
    process.stderr.write("[grok-stop-gate] stop_hook_active set; skipping the review to avoid recursion.\n");
    emitAllow();
    return;
  }

  const cwd = input.cwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();

  let config;
  try {
    config = readGateConfig(cwd);
  } catch (err) {
    // Fail closed when gate state cannot be read: never silently allow if toggle is broken.
    process.stderr.write(`[grok-stop-gate] could not read gate config: ${err.message}; blocking\n`);
    emitDecision({
      decision: "block",
      reason:
        `Grok stop-gate could not read its config (${err.message}). Fix the gate state, or disable with /grok:setup --disable-review-gate.`,
    });
    return;
  }

  if (!config.stopReviewGate) {
    emitAllow();
    return;
  }

  const storedMode = getRunMode(cwd, process.env);
  if (storedMode === "direct" && (process.env.GROK_SKILLS_MODE ?? "").trim().toLowerCase() !== "hardened") {
    process.stderr.write(
      "[grok-stop-gate] workspace prefers direct mode; stop gate still forces GROK_SKILLS_MODE=hardened for this review\n"
    );
  }

  const review = runStopReview(cwd, input);
  if (!review.ok) {
    emitDecision({ decision: "block", reason: review.reason });
    return;
  }

  process.stderr.write("[grok-stop-gate] Grok review passed structured gate; allowing the session to end.\n");
  emitAllow();
}

try {
  main();
} catch (err) {
  const detail = err && err.message ? err.message : String(err);
  process.stderr.write(
    `[grok-stop-gate] unexpected failure; failing closed with a block: ${err && err.stack ? err.stack : detail}\n`
  );
  try {
    emitDecision({
      decision: "block",
      reason:
        `The Grok stop-review gate crashed unexpectedly (${detail}). Blocking to fail closed rather than ` +
        "ending the session ungated. Re-run, or disable the gate with /grok:setup --disable-review-gate.",
    });
  } catch (emitErr) {
    const emitDetail = emitErr && emitErr.message ? emitErr.message : String(emitErr);
    process.stderr.write(`[grok-stop-gate] could not emit block decision (${emitDetail}); exiting 2 to fail closed.\n`);
    process.exit(2);
  }
}
