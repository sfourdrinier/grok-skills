#!/usr/bin/env node
// SessionStart: stamp for /grok:transfer. Codex SessionStart (--host codex)
// cheap-reconciles already-owned agents only. Claude SessionStart never
// writes ~/.codex. Setup is the mutating installer.
// Dest honors workspace prefs scope (user -> ~/.codex/agents, project ->
// <cwd>/.codex/agents). Project-scope discovery per Codex docs July 2026:
// https://developers.openai.com/codex/subagents

import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { ensureCodexAgents } from "./lib/codex-agents.mjs";
import { readAllStdinSync } from "./lib/read-stdin.mjs";
import { writeSessionStamp } from "./lib/session-stamp.mjs";

function parseHost(argv) {
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === "--host") {
      const value = String(argv[i + 1] || "").trim().toLowerCase();
      return value === "codex" || value === "claude" ? value : "claude";
    }
    if (token.startsWith("--host=")) {
      const value = token.slice("--host=".length).trim().toLowerCase();
      return value === "codex" || value === "claude" ? value : "claude";
    }
  }
  return "claude";
}

export function runSessionLifecycle({ event, host, cwd, env, pluginRoot, input }) {
  if (event === "SessionStart") {
    const sessionPath =
      input.transcript_path ||
      input.transcriptPath ||
      input.session_path ||
      env.CLAUDE_SESSION_PATH ||
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
        env
      );
    } catch {
      /* never block session start */
    }
    if (sessionPath) {
      env.GROK_CLAUDE_SESSION_PATH = sessionPath;
    }

    if (host !== "codex") {
      return { wroteCodexAgents: false };
    }

    const envRoot = (env.CLAUDE_PLUGIN_ROOT || env.PLUGIN_ROOT || "").trim();
    const root = pluginRoot;
    if (envRoot && path.resolve(envRoot) !== path.resolve(root)) {
      process.stderr.write(
        `[grok-session] using entry plugin root ${root} (ignoring stale env ${envRoot})\n`
      );
    }
    const result = ensureCodexAgents({
      pluginRoot: root,
      cwd,
      env: {
        ...env,
        CLAUDE_PLUGIN_ROOT: root,
        PLUGIN_ROOT: root,
      },
      updateManaged: true,
      force: false,
      reconcile: true,
    });
    if (Array.isArray(result.missing) && result.missing.length) {
      process.stderr.write(
        `[grok-session] Codex agents missing (${result.missing.join(", ")}). Run /grok:setup.\n`
      );
    }
    return { wroteCodexAgents: "reconcile", missing: result.missing || [] };
  }
  return { wroteCodexAgents: false };
}

const event = process.argv[2] || "SessionStart";
const host = parseHost(process.argv.slice(3));
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

runSessionLifecycle({
  event,
  host,
  cwd,
  env: process.env,
  pluginRoot: FALLBACK_PLUGIN_ROOT,
  input,
});
process.exit(0);
