// plugin/scripts/tests/codex-agents.test.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { installCodexAgents, listTemplateAgents } from "../lib/codex-agents.mjs";

const TEMPLATES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "codex-agents"
);

test("listTemplateAgents finds shipped TOML agents", () => {
  const list = listTemplateAgents(TEMPLATES);
  const names = list.map((t) => t.name);
  assert.ok(names.includes("grok-engineer-coder"));
  assert.ok(names.includes("grok-rescue"));
});

test("installCodexAgents copies templates and skips existing without force", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-home-"));
  const env = { CODEX_HOME: home };
  const first = installCodexAgents({ templatesDir: TEMPLATES, env });
  assert.equal(first.ok, true);
  assert.ok(first.installed.includes("grok-engineer-coder"));
  assert.ok(fs.existsSync(path.join(home, "agents", "grok-engineer-coder.toml")));

  const second = installCodexAgents({ templatesDir: TEMPLATES, env });
  assert.equal(second.ok, true);
  assert.equal(second.installed.length, 0);
  assert.ok(second.skipped.includes("grok-engineer-coder"));

  const forced = installCodexAgents({ templatesDir: TEMPLATES, env, force: true });
  assert.ok(forced.installed.includes("grok-engineer-coder"));
});
