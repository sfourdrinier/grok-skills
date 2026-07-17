#!/usr/bin/env node
// SessionStart: stamp for /grok:transfer + auto-ensure Codex agents.
// SessionEnd: keep last stamp for transfer.
// Codex does not register plugin agents natively (openai/codex#18988), so we
// materialize agents TOML here - zero post-install step for users.
// Dest honors workspace prefs scope (user -> ~/.codex/agents, project ->
// <cwd>/.codex/agents). Project-scope discovery per Codex docs July 2026:
// https://developers.openai.com/codex/subagents

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

  // Auto-install / refresh managed Codex agents (absolute GROK_AGENT_RUN → agents/run.mjs).
  // Silent: failures must not block the host session.
  // Prefer this script's install tree over stale env after plugin upgrade.
  const envRoot = (process.env.CLAUDE_PLUGIN_ROOT || process.env.PLUGIN_ROOT || "").trim();
  const pluginRoot = FALLBACK_PLUGIN_ROOT;
  if (envRoot && path.resolve(envRoot) !== path.resolve(pluginRoot)) {
    process.stderr.write(
      `[grok-session] using entry plugin root ${pluginRoot} (ignoring stale env ${envRoot})\n`
    );
  }
  // Scope from workspace prefs (setup --codex-agents-scope user|project).
  ensureCodexAgents({
    pluginRoot,
    cwd,
    env: {
      ...process.env,
      CLAUDE_PLUGIN_ROOT: pluginRoot,
      PLUGIN_ROOT: pluginRoot,
    },
    updateManaged: true,
    force: false,
  });
}
process.exit(0);
