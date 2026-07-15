#!/usr/bin/env node
// plugin/scripts/grok-gate.mjs
//
// Toggle or report the opt-in stop-review gate for the current workspace.
// Invoked by /grok:setup. This is plugin-local UX state only; it never touches
// the wrapper's safety boundaries.
//
//   node grok-gate.mjs --enable-review-gate
//   node grok-gate.mjs --disable-review-gate
//   node grok-gate.mjs status

import process from "node:process";

import { readGateConfig, writeGateConfig } from "./lib/gate-state.mjs";

function main() {
  const arg = process.argv[2] ?? "status";
  const cwd = process.env.CLAUDE_PROJECT_DIR || process.cwd();

  if (arg === "--enable-review-gate") {
    writeGateConfig(cwd, true);
    process.stdout.write("Grok stop-review gate: ENABLED for this workspace.\n");
    process.stdout.write("Ending a turn now runs a Grok review and blocks on a failed run or a non-pass verify verdict.\n");
    return 0;
  }

  if (arg === "--disable-review-gate") {
    writeGateConfig(cwd, false);
    process.stdout.write("Grok stop-review gate: DISABLED for this workspace.\n");
    return 0;
  }

  if (arg === "status") {
    const config = readGateConfig(cwd);
    process.stdout.write(`Grok stop-review gate: ${config.stopReviewGate ? "ENABLED" : "DISABLED"} for this workspace.\n`);
    return 0;
  }

  process.stderr.write(
    `[grok-gate] unknown argument ${JSON.stringify(arg)}. Use --enable-review-gate, --disable-review-gate, or status.\n`
  );
  return 2;
}

// F2 grok-gate-unguarded-main: wrap the invocation in the same top-level
// try/catch + actionable-message + fail-closed pattern the sibling entrypoints
// (grok-companion.mjs, stop-review-gate-hook.mjs) use, so a filesystem failure
// (e.g. an unwritable CLAUDE_PLUGIN_DATA / temp fallback while writing the gate
// config) exits with guidance and a non-zero code instead of a raw Node stack
// trace that leaves the user unsure whether the gate was toggled.
try {
  process.exit(main());
} catch (err) {
  const detail = err && err.message ? err.message : String(err);
  process.stderr.write(
    `[grok-gate] unexpected failure: ${err && err.stack ? err.stack : detail}\n` +
      "Fix: ensure the plugin data dir (CLAUDE_PLUGIN_DATA, or the OS temp fallback) is writable, then retry /grok:setup.\n"
  );
  process.exit(2);
}
