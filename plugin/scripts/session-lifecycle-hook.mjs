#!/usr/bin/env node
// SessionStart: stamp for /grok:transfer + auto-ensure Codex agents.
// SessionEnd: keep last stamp for transfer.
// Codex does not register plugin agents natively (openai/codex#18988), so we
// materialize ~/.codex/agents/*.toml here — zero post-install step for users.

import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { ensureCodexAgents } from "./lib/codex-agents.mjs";
import { readAllStdinSync } from "./lib/read-stdin.mjs";
import { writeSessionStamp } from "./lib/session-stamp.mjs";

const event = process.argv[2] || "SessionStart";
let input = {};
try {
  const raw = readAllStdinSync().toString("utf8").trim();
  input = raw ? JSON.parse(raw) : {};
} catch {
  input = {};
}

const cwd = input.cwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const FALLBACK_PLUGIN_ROOT = path.resolve(SCRIPT_DIR, "..");

if (event === "SessionStart") {
  const sessionPath =
    input.transcript_path ||
    input.transcriptPath ||
    input.session_path ||
    process.env.CLAUDE_SESSION_PATH ||
    null;
  try {
    writeSessionStamp(
      cwd,
      {
        event,
        at: new Date().toISOString(),
        cwd,
        transcript_path: sessionPath,
      },
      process.env
    );
  } catch {
    /* never block session start */
  }
  if (sessionPath) {
    process.env.GROK_CLAUDE_SESSION_PATH = sessionPath;
  }

  // Auto-install / refresh managed Codex agents (absolute companion path).
  // Silent: failures must not block the host session.
  const pluginRoot =
    (process.env.CLAUDE_PLUGIN_ROOT || process.env.PLUGIN_ROOT || "").trim() ||
    FALLBACK_PLUGIN_ROOT;
  ensureCodexAgents({
    pluginRoot,
    env: process.env,
    updateManaged: true,
    force: false,
  });
}
process.exit(0);
