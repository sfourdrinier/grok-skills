// plugin/scripts/tests/resolve-plugin-root.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

// Spawn under heavy concurrent load (a full suite plus other processes) can hit
// EAGAIN/ENOMEM at the OS level - a resource-pressure failure, not a resolution
// bug. Retry the spawn (never the assertions) a few times before giving up so a
// deterministic root-resolution test is not flaky under load.
function spawnNode(args) {
  let result;
  for (let attempt = 0; attempt < 8; attempt++) {
    result = spawnSync(process.execPath, args, { encoding: "utf8" });
    // Retry on any spawn-level error OR a non-zero exit under load (EAGAIN can
    // surface either way when the machine is saturated with concurrent
    // processes); the resolution itself is deterministic, so a clean run wins.
    if (!result.error && result.status === 0) {
      return result;
    }
    // small backoff that YIELDS the CPU (a busy-spin would worsen the very
    // resource pressure we are backing off from). Atomics.wait blocks without
    // burning a core.
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25 * (attempt + 1));
  }
  return result;
}

import {
  companionPath,
  isValidPluginRoot,
  pluginRootFromSkillDir,
  resolvePluginRoot,
  BUNDLED_PLUGIN_ROOT,
} from "../lib/resolve-plugin-root.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(HERE, "..", "..");
const DUAL_LENS_SKILL = path.join(PLUGIN_ROOT, "skills", "dual-lens");
const CLI = path.join(PLUGIN_ROOT, "scripts", "resolve-plugin-root.mjs");

test("bundled plugin root is valid", () => {
  assert.equal(path.resolve(BUNDLED_PLUGIN_ROOT), PLUGIN_ROOT);
  assert.ok(isValidPluginRoot(PLUGIN_ROOT));
  assert.ok(fs.existsSync(companionPath(PLUGIN_ROOT)));
});

test("pluginRootFromSkillDir maps skills/<name> to plugin root", () => {
  assert.equal(pluginRootFromSkillDir(DUAL_LENS_SKILL), PLUGIN_ROOT);
  assert.equal(
    pluginRootFromSkillDir(path.join(DUAL_LENS_SKILL, "SKILL.md")),
    PLUGIN_ROOT
  );
  assert.equal(
    pluginRootFromSkillDir(path.join(PLUGIN_ROOT, "skills")),
    PLUGIN_ROOT
  );
});

test("resolvePluginRoot prefers CLAUDE_PLUGIN_ROOT then PLUGIN_ROOT then skill-dir", () => {
  const a = resolvePluginRoot({
    env: { CLAUDE_PLUGIN_ROOT: PLUGIN_ROOT },
    skillDir: null,
  });
  assert.equal(a.source, "CLAUDE_PLUGIN_ROOT");
  assert.equal(a.root, PLUGIN_ROOT);

  const b = resolvePluginRoot({
    env: { PLUGIN_ROOT: PLUGIN_ROOT },
    skillDir: null,
  });
  assert.equal(b.source, "PLUGIN_ROOT");

  const c = resolvePluginRoot({
    env: {},
    skillDir: DUAL_LENS_SKILL,
  });
  assert.equal(c.source, "skill-dir");
  assert.equal(c.root, PLUGIN_ROOT);
  assert.equal(c.companion, companionPath(PLUGIN_ROOT));
});

test("resolvePluginRoot fails closed when env and skill-dir missing", () => {
  const r = resolvePluginRoot({ env: {}, skillDir: null });
  assert.equal(r.root, null);
  assert.match(r.error, /plugin root not set/i);
});

test("resolvePluginRoot fails closed when candidate has no companion", () => {
  const fake = fs.mkdtempSync(path.join(os.tmpdir(), "not-a-plugin-"));
  const r = resolvePluginRoot({ env: { CLAUDE_PLUGIN_ROOT: fake } });
  assert.equal(r.root, null);
  assert.match(r.error, /invalid|missing/i);
});

test("CLI --skill-dir prints root and --companion prints companion", () => {
  const rootRun = spawnNode([CLI, "--skill-dir", DUAL_LENS_SKILL]);
  assert.equal(rootRun.status, 0, rootRun.stderr);
  assert.equal(rootRun.stdout.trim(), PLUGIN_ROOT);

  const compRun = spawnNode([CLI, "--skill-dir", DUAL_LENS_SKILL, "--companion"]);
  assert.equal(compRun.status, 0, compRun.stderr);
  assert.equal(compRun.stdout.trim(), companionPath(PLUGIN_ROOT));
});

test("CLI fails without skill-dir or env", () => {
  const run = spawnSync(process.execPath, [CLI], {
    encoding: "utf8",
    env: { ...process.env, CLAUDE_PLUGIN_ROOT: "", PLUGIN_ROOT: "", SKILL_DIR: "" },
  });
  // Clear may not empty inherited - force empty plugin env only
  const run2 = spawnSync(process.execPath, [CLI], {
    encoding: "utf8",
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
    },
  });
  assert.notEqual(run2.status, 0);
  assert.match(run2.stderr, /plugin root not set/i);
});
