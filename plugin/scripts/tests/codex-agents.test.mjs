// plugin/scripts/tests/codex-agents.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ensureCodexAgents,
  installCodexAgents,
  isManagedAgentBody,
  listTemplateAgents,
  materializeAgentBody,
  resolveCompanionPath,
  shellSingleQuote,
} from "../lib/codex-agents.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATES = path.resolve(HERE, "..", "..", "codex-agents");
const PLUGIN_ROOT = path.resolve(HERE, "..", "..");

test("listTemplateAgents finds shipped TOML agents", () => {
  const list = listTemplateAgents(TEMPLATES);
  const names = list.map((t) => t.name);
  assert.ok(names.includes("grok-engineer-coder"));
  assert.ok(names.includes("grok-rescue"));
});

test("shipped templates use absolute companion placeholder", () => {
  for (const t of listTemplateAgents(TEMPLATES)) {
    const body = fs.readFileSync(t.source, "utf8");
    assert.ok(
      body.includes("__GROK_COMPANION_Q__"),
      `${t.name} missing __GROK_COMPANION_Q__`
    );
    assert.ok(!body.includes("${PLUGIN_ROOT"), `${t.name} still uses PLUGIN_ROOT`);
    assert.ok(
      !body.includes("${CLAUDE_PLUGIN_ROOT"),
      `${t.name} still uses CLAUDE_PLUGIN_ROOT`
    );
  }
});

test("shellSingleQuote escapes embedded single quotes", () => {
  assert.equal(shellSingleQuote("/tmp/x"), "'/tmp/x'");
  assert.equal(shellSingleQuote("/tmp/o'brien"), `'/tmp/o'\\''brien'`);
});

test("materializeAgentBody injects absolute companion and managed header", () => {
  const src = fs.readFileSync(
    path.join(TEMPLATES, "grok-engineer-coder.toml"),
    "utf8"
  );
  const companion = "/cache/grok/1.2.1/scripts/grok-companion.mjs";
  const body = materializeAgentBody(src, companion);
  assert.ok(isManagedAgentBody(body));
  assert.ok(body.includes(`companion: ${companion}`));
  assert.ok(body.includes(`GROK_COMPANION='${companion}'`));
  assert.ok(!body.includes("__GROK_COMPANION_Q__"));
});

test("installCodexAgents writes managed agents with absolute companion", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  const first = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(first.ok, true);
  assert.ok(first.installed.includes("grok-engineer-coder"));
  assert.ok(first.installed.includes("grok-rescue"));

  const dest = path.join(home, "agents", "grok-engineer-coder.toml");
  const body = fs.readFileSync(dest, "utf8");
  const companion = resolveCompanionPath(PLUGIN_ROOT);
  assert.ok(isManagedAgentBody(body));
  assert.ok(body.includes(companion));
  assert.ok(body.includes(`GROK_COMPANION=${shellSingleQuote(companion)}`));
  assert.ok(fs.existsSync(companion));
});

test("installCodexAgents skips identical managed files; updates on companion drift", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  const first = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(first.ok, true);

  const second = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(second.ok, true);
  assert.equal(second.installed.length, 0);
  assert.equal(second.updated.length, 0);
  assert.ok(second.skipped.includes("grok-engineer-coder"));

  // Simulate plugin upgrade to a new cache root with a real companion binary.
  const fakeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "plugin-root-"));
  const scriptsDir = path.join(fakeRoot, "scripts");
  fs.mkdirSync(scriptsDir, { recursive: true });
  fs.writeFileSync(path.join(scriptsDir, "grok-companion.mjs"), "// stub\n");

  const third = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: fakeRoot,
    updateManaged: true,
  });
  assert.equal(third.ok, true);
  assert.ok(third.updated.includes("grok-engineer-coder"));
  const body = fs.readFileSync(
    path.join(home, "agents", "grok-engineer-coder.toml"),
    "utf8"
  );
  assert.ok(body.includes(path.join(fakeRoot, "scripts", "grok-companion.mjs")));
});

test("installCodexAgents does not overwrite unmanaged user agents without force", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  const agentsDir = path.join(home, "agents");
  fs.mkdirSync(agentsDir, { recursive: true });
  const custom = "# my custom agent\nname = \"grok-engineer-coder\"\n";
  fs.writeFileSync(path.join(agentsDir, "grok-engineer-coder.toml"), custom);

  const result = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
    force: false,
  });
  assert.equal(result.ok, true);
  assert.ok(result.skippedUser.includes("grok-engineer-coder"));
  assert.equal(
    fs.readFileSync(path.join(agentsDir, "grok-engineer-coder.toml"), "utf8"),
    custom
  );

  const forced = installCodexAgents({
    templatesDir: TEMPLATES,
    env,
    pluginRoot: PLUGIN_ROOT,
    force: true,
  });
  assert.ok(forced.updated.includes("grok-engineer-coder"));
  assert.ok(
    isManagedAgentBody(
      fs.readFileSync(path.join(agentsDir, "grok-engineer-coder.toml"), "utf8")
    )
  );
});

test("ensureCodexAgents never throws", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const result = ensureCodexAgents({
    templatesDir: "/nonexistent-templates-dir",
    env: { CODEX_HOME: home },
    pluginRoot: PLUGIN_ROOT,
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.length > 0);
});
