// plugin/scripts/tests/skill-run.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

import {
  pluginRootFromPluginEntryUrl,
  pluginRootFromSkillEntryUrl,
} from "../lib/skill-run.mjs";
import { BUNDLED_PLUGIN_ROOT } from "../lib/resolve-plugin-root.mjs";
import { companionIsolation } from "./helpers/fake-wrapper.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN = path.resolve(HERE, "..", "..");
const PREFLIGHT_RUN = path.join(PLUGIN, "skills", "preflight", "run.mjs");
const DUAL_LENS_RUN = path.join(PLUGIN, "skills", "dual-lens", "run.mjs");
const AGENTS_RUN = path.join(PLUGIN, "agents", "run.mjs");

/** Minimal host env + isolation defaults (never real XDG / workspace registry). */
function isolatedBareEnv(extra = {}) {
  const iso = companionIsolation({
    env: {
      // Drop ambient CLAUDE_PLUGIN_ROOT etc. by starting from isolation only.
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      USER: process.env.USER,
      ...extra,
    },
  });
  // companionIsolation merges process.env first; rebuild a strict allowlist.
  const env = {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    USER: process.env.USER,
    XDG_STATE_HOME: iso.env.XDG_STATE_HOME,
    CLAUDE_PLUGIN_DATA: iso.env.CLAUDE_PLUGIN_DATA,
    TMPDIR: iso.env.TMPDIR,
    TMP: iso.env.TMP,
    TEMP: iso.env.TEMP,
    ...extra,
  };
  return { env, cwd: iso.cwd, cleanup: iso.cleanup };
}

/**
 * Assert self-locating entry reached the real wrapper and produced an envelope.
 *
 * These tests prove plugin-root resolution and companion spawn - not host grok
 * readiness. On CI (no ~/.grok/bin/grok) preflight returns failure /
 * tool-unavailable with exit 1; with a binary it may return success / 0.
 * Either path proves the entry located the install and ran the pipeline.
 */
function assertSelfLocatingPreflight(result, { forbidExit = [] } = {}) {
  const text = `${result.stderr}\n${result.stdout}`;
  assert.notEqual(result.status, 127, `plugin-root failure:\n${text}`);
  for (const code of forbidExit) {
    assert.notEqual(result.status, code, `unexpected exit ${code}:\n${text}`);
  }
  assert.doesNotMatch(text, /plugin root not set/i);
  assert.doesNotMatch(text, /invalid plugin root/i);
  assert.doesNotMatch(text, /skill-run: failed to spawn companion/i);
  // Envelope on stdout: mode preflight + classified status (success or failure).
  assert.match(result.stdout, /"mode":\s*"preflight"/, text);
  assert.match(result.stdout, /"status":\s*"(success|failure)"/, text);
  // Real pipeline exit codes for preflight: 0 (ready) or 1 (classified fail).
  assert.ok(
    result.status === 0 || result.status === 1,
    `expected exit 0 or 1, got ${result.status}:\n${text}`
  );
  if (result.status === 0) {
    assert.match(result.stdout, /"status":\s*"success"/, text);
  } else {
    assert.match(result.stdout, /"status":\s*"failure"/, text);
  }
}

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
  const iso = isolatedBareEnv();
  try {
    const result = spawnSync(process.execPath, [AGENTS_RUN, "preflight"], {
      encoding: "utf8",
      cwd: iso.cwd,
      env: iso.env,
    });
    assertSelfLocatingPreflight(result);
  } finally {
    iso.cleanup();
  }
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
  const iso = isolatedBareEnv();
  try {
    const result = spawnSync(process.execPath, [PREFLIGHT_RUN, "preflight"], {
      encoding: "utf8",
      cwd: iso.cwd,
      env: iso.env,
    });
    assertSelfLocatingPreflight(result);
  } finally {
    iso.cleanup();
  }
});

test("run.mjs forces entry-derived root over stale CLAUDE_PLUGIN_ROOT", () => {
  // Stale env pointing at a non-install path must not break self-locating entry
  // or load a different wrapper tree.
  const iso = isolatedBareEnv({
    CLAUDE_PLUGIN_ROOT: "/tmp/stale-grok-plugin-root-does-not-exist",
    PLUGIN_ROOT: "/tmp/stale-grok-plugin-root-does-not-exist",
  });
  try {
    const result = spawnSync(process.execPath, [PREFLIGHT_RUN, "preflight"], {
      encoding: "utf8",
      cwd: iso.cwd,
      env: iso.env,
    });
    assertSelfLocatingPreflight(result);
  } finally {
    iso.cleanup();
  }
});

test("run.mjs prefers entry tree over a second valid plugin root in env", () => {
  // Upgrade skew: env still points at an older *valid* install. Entry must win.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stale-plugin-"));
  const staleScripts = path.join(tmp, "scripts");
  fs.mkdirSync(staleScripts, { recursive: true });
  // Minimal companion that would succeed but prove we did NOT use it: exits 42.
  fs.writeFileSync(
    path.join(staleScripts, "grok-companion.mjs"),
    "#!/usr/bin/env node\nprocess.exit(42);\n",
    "utf8"
  );
  fs.mkdirSync(path.join(tmp, "wrapper", "scripts"), { recursive: true });
  fs.writeFileSync(path.join(tmp, "wrapper", "scripts", "grok_agent.py"), "# stale\n", "utf8");
  const iso = isolatedBareEnv({
    CLAUDE_PLUGIN_ROOT: tmp,
    PLUGIN_ROOT: tmp,
  });
  try {
    const result = spawnSync(process.execPath, [PREFLIGHT_RUN, "preflight"], {
      encoding: "utf8",
      cwd: iso.cwd,
      env: iso.env,
    });
    // Stale companion would exit 42. Real entry tree yields 0 or classified 1.
    assertSelfLocatingPreflight(result, { forbidExit: [42] });
  } finally {
    iso.cleanup();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("run.mjs invalid mode is not a plugin-root failure", () => {
  const iso = isolatedBareEnv();
  try {
    const result = spawnSync(process.execPath, [DUAL_LENS_RUN, "not-a-real-mode"], {
      encoding: "utf8",
      cwd: iso.cwd,
      env: iso.env,
    });
    const text = `${result.stderr}\n${result.stdout}`;
    assert.notEqual(result.status, 127);
    assert.doesNotMatch(text, /plugin root not set/i);
    assert.doesNotMatch(text, /invalid plugin root/i);
  } finally {
    iso.cleanup();
  }
});
