#!/usr/bin/env node
// Records Claude session path for /grok:transfer (SessionStart).
// Workspace-keyed stamp (not a single global latest.json).

import process from "node:process";
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

if (event === "SessionStart") {
  const sessionPath =
    input.transcript_path ||
    input.transcriptPath ||
    input.session_path ||
    process.env.CLAUDE_SESSION_PATH ||
    null;
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
  if (sessionPath) {
    process.env.GROK_CLAUDE_SESSION_PATH = sessionPath;
  }
}
// SessionEnd: keep last stamp for transfer
process.exit(0);
