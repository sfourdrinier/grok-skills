// plugin/scripts/tests/bin-shim.test.mjs
//
// Contract tests for plugin/bin/grok-skills: self-locating PATH shim that
// forwards argv to scripts/grok-companion.mjs with exit-code passthrough.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { createJob } from "../lib/jobs.mjs";
import { companionIsolation, makeFakeWrapper } from "./helpers/fake-wrapper.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN = path.resolve(HERE, "..", "..");
const SHIM = path.join(PLUGIN, "bin", "grok-skills");

const RID = "20260717T000000Z-abc123";

function runShim(argv, { env = {}, cwd } = {}) {
  const iso = companionIsolation({ env, cwd });
  try {
    const result = spawnSync(SHIM, argv, {
      cwd: iso.cwd,
      encoding: "utf8",
      env: iso.env,
    });
    return {
      code: typeof result.status === "number" ? result.status : 1,
      stdout: result.stdout || "",
      stderr: result.stderr || "",
      error: result.error,
      iso,
    };
  } catch (err) {
    iso.cleanup();
    throw err;
  }
}

test("bin/grok-skills jobs exits 0 and prints jobs table header", () => {
  assert.ok(fs.existsSync(SHIM), `shim missing at ${SHIM}`);
  const iso = companionIsolation({});
  try {
    // Seed one job so formatJobsTable emits the column header row.
    createJob(
      iso.cwd,
      { kind: "review", mode: "review", runMode: "hardened" },
      { CLAUDE_PLUGIN_DATA: iso.env.CLAUDE_PLUGIN_DATA }
    );
    const res = runShim(["jobs"], {
      cwd: iso.cwd,
      env: { CLAUDE_PLUGIN_DATA: iso.env.CLAUDE_PLUGIN_DATA },
    });
    try {
      assert.equal(res.error, undefined, `spawn failed: ${res.error}`);
      assert.equal(res.code, 0, `expected exit 0, got ${res.code}\nstderr: ${res.stderr}`);
      assert.match(
        res.stdout,
        /ID\s+KIND\s+STATUS\s+MODE\s+RUN\s+UPDATED/,
        `jobs table header missing:\n${res.stdout}`
      );
    } finally {
      res.iso.cleanup();
    }
  } finally {
    iso.cleanup();
  }
});

test("bin/grok-skills propagates nonzero exit for unknown wrapper mode", () => {
  assert.ok(fs.existsSync(SHIM), `shim missing at ${SHIM}`);
  const { env, cleanup } = makeFakeWrapper({});
  try {
    // Unregistered mode -> fake wrapper exit 2 (same probe as fake-wrapper tests).
    const res = runShim(["status", "--run-id", RID], { env });
    try {
      assert.equal(res.error, undefined, `spawn failed: ${res.error}`);
      assert.notEqual(res.code, 0, `expected nonzero exit, got 0\nstdout: ${res.stdout}`);
    } finally {
      res.iso.cleanup();
    }
  } finally {
    cleanup();
  }
});
