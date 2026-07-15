// plugin/scripts/tests/grok-gate.test.mjs
//
// F2 grok-gate-unguarded-main: grok-gate.mjs must fail closed with an actionable
// message on an unexpected throw (e.g. an unwritable plugin-data dir), mirroring
// its sibling entrypoints -- never a raw Node stack trace that leaves the user
// unsure whether the gate toggled. Also covers the normal status happy path.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const GATE_SCRIPT = path.resolve(SCRIPT_DIR, "..", "grok-gate.mjs");

function runGate(args, env) {
  return spawnSync(process.execPath, [GATE_SCRIPT, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

test("grok-gate status reports DISABLED by default (happy path, exit 0)", () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-data-"));
  try {
    const result = runGate(["status"], { CLAUDE_PLUGIN_DATA: dataDir, CLAUDE_PROJECT_DIR: dataDir });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /DISABLED/);
  } finally {
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
});

test("grok-gate fails closed with an actionable message when the state dir is unwritable", () => {
  // Point CLAUDE_PLUGIN_DATA at a FILE so mkdirSync(dir under it) throws ENOTDIR
  // deep inside writeGateConfig -- the exact class of filesystem failure the
  // top-level guard must catch.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-nowrite-"));
  const blockingFile = path.join(tmp, "not-a-dir");
  fs.writeFileSync(blockingFile, "x");
  try {
    const result = runGate(["--enable-review-gate"], {
      CLAUDE_PLUGIN_DATA: blockingFile,
      CLAUDE_PROJECT_DIR: tmp,
    });
    assert.notEqual(result.status, 0, "an unwritable state dir must not exit 0");
    assert.equal(result.status, 2);
    assert.match(result.stderr, /\[grok-gate\] unexpected failure/);
    assert.match(result.stderr, /Fix:/);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
