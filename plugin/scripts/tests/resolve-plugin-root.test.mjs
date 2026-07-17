// plugin/scripts/tests/resolve-plugin-root.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";


import {
  companionPath,
  isValidPluginRoot,
  pluginRootFromSkillDir,
  resolvePluginRoot,
  BUNDLED_PLUGIN_ROOT,
} from "../lib/resolve-plugin-root.mjs";
import { main as resolveCli } from "../lib/resolve-plugin-root.mjs";

// Run the CLI main() IN-PROCESS, capturing stdout. Avoids spawning a
// subprocess, which was flaky under heavy concurrent machine load (EAGAIN on
// spawn) - the resolution logic is deterministic and needs no child process.
function runCli(argv, env = {}) {
  const out = [];
  const err = [];
  const origOut = process.stdout.write.bind(process.stdout);
  const origErr = process.stderr.write.bind(process.stderr);
  process.stdout.write = (s) => { out.push(String(s)); return true; };
  process.stderr.write = (s) => { err.push(String(s)); return true; };
  let status;
  try {
    status = resolveCli(argv, env);
  } finally {
    process.stdout.write = origOut;
    process.stderr.write = origErr;
  }
  return { status, stdout: out.join(""), stderr: err.join("") };
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(HERE, "..", "..");
const DUAL_LENS_SKILL = path.join(PLUGIN_ROOT, "skills", "dual-lens");

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

test("CLI main prints root and --companion prints companion (in-process)", () => {
  const rootRun = runCli(["--skill-dir", DUAL_LENS_SKILL]);
  assert.equal(rootRun.status, 0);
  assert.equal(rootRun.stdout.trim(), PLUGIN_ROOT);

  const compRun = runCli(["--skill-dir", DUAL_LENS_SKILL, "--companion"]);
  assert.equal(compRun.status, 0);
  assert.equal(compRun.stdout.trim(), companionPath(PLUGIN_ROOT));
});

test("CLI fails without skill-dir or env (in-process)", () => {
  const run = runCli([], {});
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, /plugin root not set/i);
});
