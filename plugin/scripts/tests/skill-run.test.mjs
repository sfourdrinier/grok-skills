// plugin/scripts/tests/skill-run.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

import {
  pluginRootFromPluginEntryUrl,
  pluginRootFromSkillEntryUrl,
} from "../lib/skill-run.mjs";
import { BUNDLED_PLUGIN_ROOT } from "../lib/resolve-plugin-root.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN = path.resolve(HERE, "..", "..");
const PREFLIGHT_RUN = path.join(PLUGIN, "skills", "preflight", "run.mjs");
const DUAL_LENS_RUN = path.join(PLUGIN, "skills", "dual-lens", "run.mjs");
const AGENTS_RUN = path.join(PLUGIN, "agents", "run.mjs");

const bareEnv = {
  PATH: process.env.PATH,
  HOME: process.env.HOME,
  TMPDIR: process.env.TMPDIR,
  USER: process.env.USER,
};

test("pluginRootFromSkillEntryUrl maps skills/<name>/run.mjs to plugin root", () => {
  const url = pathToFileURL(PREFLIGHT_RUN).href;
  assert.equal(pluginRootFromSkillEntryUrl(url), PLUGIN);
  assert.equal(pluginRootFromSkillEntryUrl(url), BUNDLED_PLUGIN_ROOT);
});

test("pluginRootFromPluginEntryUrl maps agents/run.mjs to plugin root", () => {
  assert.ok(fs.existsSync(AGENTS_RUN));
  const url = pathToFileURL(AGENTS_RUN).href;
  assert.equal(pluginRootFromPluginEntryUrl(url), PLUGIN);
});

test("agents/run.mjs works with no CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT", () => {
  const result = spawnSync(process.execPath, [AGENTS_RUN, "preflight"], {
    encoding: "utf8",
    env: bareEnv,
  });
  assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`);
  assert.match(result.stdout, /"status": "success"/);
});

test("every skill has a self-locating run.mjs", () => {
  const skillsDir = path.join(PLUGIN, "skills");
  for (const name of fs.readdirSync(skillsDir)) {
    const dir = path.join(skillsDir, name);
    if (!fs.statSync(dir).isDirectory()) continue;
    const run = path.join(dir, "run.mjs");
    assert.ok(fs.existsSync(run), `missing ${run}`);
    const body = fs.readFileSync(run, "utf8");
    assert.match(body, /runFromSkillEntry/);
    assert.match(body, /import\.meta\.url/);
    const skill = fs.readFileSync(path.join(dir, "SKILL.md"), "utf8");
    assert.match(skill, /\$SKILL_BASE\/run\.mjs/);
    assert.ok(!skill.includes("PLUGIN_ROOT:?plugin root not set"));
  }
});

test("preflight run.mjs works with no CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT", () => {
  const result = spawnSync(process.execPath, [PREFLIGHT_RUN, "preflight"], {
    encoding: "utf8",
    env: bareEnv,
  });
  assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`);
  assert.match(result.stdout, /"status": "success"/);
});

test("run.mjs invalid mode is not a plugin-root failure", () => {
  const result = spawnSync(process.execPath, [DUAL_LENS_RUN, "not-a-real-mode"], {
    encoding: "utf8",
    env: bareEnv,
  });
  const text = `${result.stderr}\n${result.stdout}`;
  assert.notEqual(result.status, 127);
  assert.doesNotMatch(text, /plugin root not set/i);
  assert.doesNotMatch(text, /invalid plugin root/i);
});
