// plugin/scripts/tests/host-lifecycle.test.mjs
//
// F03-F05 / issue #14: host-specific SessionStart, Codex SessionEnd timeout,
// generated TOML `name` keys, writer vs consultant sandbox, setup-as-installer.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  installCodexAgents,
  materializeAgentBody,
} from "../lib/codex-agents.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(HERE, "..", "..");
const HOOK = path.resolve(HERE, "..", "session-lifecycle-hook.mjs");
const CLAUDE_HOOKS = path.resolve(PLUGIN_ROOT, "hooks", "hooks.json");
const CODEX_HOOKS = path.resolve(PLUGIN_ROOT, "hooks", "codex.hooks.json");
const CODEX_PLUGIN = path.resolve(PLUGIN_ROOT, ".codex-plugin", "plugin.json");
const TEMPLATES = path.resolve(PLUGIN_ROOT, "codex-agents");

function runHook(args, env, stdinObj) {
  return spawnSync(process.execPath, [HOOK, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env },
    input: JSON.stringify(stdinObj ?? {}),
  });
}

test("issue #14: materialized TOML has an independent name key", () => {
  for (const name of ["grok-engineer-coder", "grok-rescue"]) {
    const src = fs.readFileSync(path.join(TEMPLATES, `${name}.toml`), "utf8");
    const body = materializeAgentBody(
      src,
      "/cache/grok/agents/run.mjs",
      "/cache/grok/scripts/grok-companion.mjs"
    );
    assert.match(
      body,
      new RegExp(`\\nname\\s*=\\s*"${name}"`),
      `${name} name must not be concatenated onto the managed header`
    );
    assert.doesNotMatch(body, /onlyname\s*=/);
  }
});

test("issue #14: installCodexAgents writes parseable name keys", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-name-"));
  const result = installCodexAgents({
    templatesDir: TEMPLATES,
    env: { CODEX_HOME: home },
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(result.ok, true);
  for (const name of ["grok-engineer-coder", "grok-rescue"]) {
    const body = fs.readFileSync(path.join(home, "agents", `${name}.toml`), "utf8");
    assert.match(body, new RegExp(`\\nname\\s*=\\s*"${name}"`));
  }
});

test("Codex hooks SessionEnd timeout is 3 (no clamp warning)", () => {
  assert.equal(fs.existsSync(CODEX_HOOKS), true, "Codex must have its own hooks file");
  const hooks = JSON.parse(fs.readFileSync(CODEX_HOOKS, "utf8"));
  const timeout = hooks.hooks.SessionEnd[0].hooks[0].timeout;
  assert.equal(timeout, 3);
  const startCmd = hooks.hooks.SessionStart[0].hooks[0].command;
  assert.match(startCmd, /--host['"\s=]+codex/);
});

test("Claude hooks SessionEnd timeout stays 5 and SessionStart is --host claude", () => {
  const hooks = JSON.parse(fs.readFileSync(CLAUDE_HOOKS, "utf8"));
  assert.equal(hooks.hooks.SessionEnd[0].hooks[0].timeout, 5);
  const startCmd = hooks.hooks.SessionStart[0].hooks[0].command;
  assert.match(startCmd, /--host['"\s=]+claude/);
  assert.doesNotMatch(startCmd, /Codex agents/);
});

test("Codex plugin.json points at codex.hooks.json", () => {
  const manifest = JSON.parse(fs.readFileSync(CODEX_PLUGIN, "utf8"));
  assert.equal(manifest.hooks, "./hooks/codex.hooks.json");
  assert.equal(manifest.version, "2.1.0");
});

test("grok-engineer-coder is workspace-write; grok-rescue is read-only", () => {
  const coder = fs.readFileSync(path.join(TEMPLATES, "grok-engineer-coder.toml"), "utf8");
  const rescue = fs.readFileSync(path.join(TEMPLATES, "grok-rescue.toml"), "utf8");
  assert.match(coder, /sandbox_mode\s*=\s*"workspace-write"/);
  assert.doesNotMatch(coder, /sandbox_mode\s*=\s*"danger-full-access"/);
  assert.match(rescue, /sandbox_mode\s*=\s*"read-only"/);
});

test("Claude SessionStart writes zero files under CODEX_HOME", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-claude-ss-"));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "ws-claude-ss-"));
  const pdata = path.join(cwd, "pdata");
  const res = runHook(["SessionStart", "--host", "claude"], {
    CODEX_HOME: home,
    CLAUDE_PLUGIN_DATA: pdata,
    CLAUDE_PROJECT_DIR: cwd,
  }, { cwd });
  assert.equal(res.status, 0, res.stderr);
  const agents = path.join(home, "agents");
  assert.equal(fs.existsSync(agents), false, "Claude SessionStart must not create ~/.codex/agents");
});

test("Codex SessionStart does not install missing agents (setup is the installer)", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-recon-"));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "ws-codex-ss-"));
  const res = runHook(["SessionStart", "--host", "codex"], {
    CODEX_HOME: home,
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata"),
    CLAUDE_PROJECT_DIR: cwd,
  }, { cwd });
  assert.equal(res.status, 0, res.stderr);
  assert.equal(
    fs.existsSync(path.join(home, "agents", "grok-engineer-coder.toml")),
    false,
    "missing agents stay missing until /grok:setup"
  );
  assert.match(
    String(res.stderr),
    /\/grok:setup/,
    "Codex SessionStart must hint to run setup when agents are missing"
  );
});

test("installCodexAgents reports a conflict for a user-edited managed template", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-conflict-"));
  const env = { CODEX_HOME: home };
  const first = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(first.ok, true);
  const dest = path.join(home, "agents", "grok-engineer-coder.toml");
  const original = fs.readFileSync(dest, "utf8");
  fs.writeFileSync(
    dest,
    original.replace("You are grok-engineer-coder.", "USER EDIT KEEP THIS")
  );
  const second = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
    force: false,
    updateManaged: true,
  });
  assert.ok(
    (second.conflicts || []).includes("grok-engineer-coder") ||
      (second.skippedUser || []).includes("grok-engineer-coder"),
    `expected conflict, got ${JSON.stringify({
      skippedUser: second.skippedUser,
      skipped: second.skipped,
      updated: second.updated,
      conflicts: second.conflicts,
    })}`
  );
  assert.ok(fs.readFileSync(dest, "utf8").includes("USER EDIT KEEP THIS"));
});
