// plugin/scripts/tests/grok-companion.test.mjs
//
// Verifies the companion resolves the wrapper and forwards argv + stdout + exit
// code unchanged. Run with: node --test plugin/scripts/tests/

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  candidateWrapperPaths,
  resolveWrapperPath,
  wrapperNotFoundMessage
} from "../lib/wrapper.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(SCRIPT_DIR, "..", "grok-companion.mjs");

test("candidateWrapperPaths orders override (when allowed), CLAUDE_PLUGIN_ROOT, PLUGIN_ROOT, then derived", () => {
  const candidates = candidateWrapperPaths({
    GROK_AGENT_WRAPPER: "/tmp/custom/grok_agent.py",
    GROK_ALLOW_WRAPPER_OVERRIDE: "1",
    CLAUDE_PLUGIN_ROOT: "/opt/plugins/cache/grok",
    PLUGIN_ROOT: "/opt/codex/plugins/grok"
  });
  assert.equal(candidates[0], path.resolve("/tmp/custom/grok_agent.py"));
  assert.equal(
    candidates[1],
    path.resolve("/opt/plugins/cache/grok", "wrapper/scripts/grok_agent.py")
  );
  assert.equal(
    candidates[2],
    path.resolve("/opt/codex/plugins/grok", "wrapper/scripts/grok_agent.py")
  );
  // The derived fallback is always present as the last resort.
  assert.ok(candidates[candidates.length - 1].endsWith("wrapper/scripts/grok_agent.py"));
});

test("candidateWrapperPaths ignores GROK_AGENT_WRAPPER without allow flag", () => {
  const candidates = candidateWrapperPaths({
    GROK_AGENT_WRAPPER: "/tmp/custom/grok_agent.py",
    CLAUDE_PLUGIN_ROOT: "/opt/plugins/cache/grok"
  });
  assert.equal(
    candidates[0],
    path.resolve("/opt/plugins/cache/grok", "wrapper/scripts/grok_agent.py")
  );
  assert.ok(!candidates.some((c) => c.includes("/tmp/custom/grok_agent.py")));
});

test("candidateWrapperPaths omits absent env candidates", () => {
  const candidates = candidateWrapperPaths({});
  // With no override and no plugin root env, only the derived fallback remains.
  assert.equal(candidates.length, 1);
  assert.ok(candidates[0].endsWith("wrapper/scripts/grok_agent.py"));
});

test("resolveWrapperPath finds the bundled plugin wrapper via the derived fallback", () => {
  const resolved = resolveWrapperPath({});
  assert.ok(resolved, "expected the bundled wrapper to resolve");
  assert.ok(resolved.endsWith(path.join("wrapper", "scripts", "grok_agent.py")));
});

test("resolveWrapperPath ignores GROK_AGENT_WRAPPER without allow flag", () => {
  const bundled = resolveWrapperPath({});
  const resolved = resolveWrapperPath({
    GROK_AGENT_WRAPPER: "/tmp/definitely-not-a-real-wrapper.py"
  });
  assert.equal(resolved, path.resolve(bundled));
});

test("resolveWrapperPath honors GROK_AGENT_WRAPPER when allow flag is set", () => {
  const real = resolveWrapperPath({});
  const resolved = resolveWrapperPath({
    GROK_AGENT_WRAPPER: real,
    GROK_ALLOW_WRAPPER_OVERRIDE: "1"
  });
  assert.equal(resolved, path.resolve(real));
});

test("wrapperNotFoundMessage is actionable and points at /grok:setup", () => {
  const message = wrapperNotFoundMessage({
    GROK_AGENT_WRAPPER: "/nope/grok_agent.py",
    GROK_ALLOW_WRAPPER_OVERRIDE: "1"
  });
  assert.match(message, /\/grok:setup/);
  assert.match(message, /GROK_AGENT_WRAPPER/);
  assert.match(message, /GROK_ALLOW_WRAPPER_OVERRIDE/);
});

test("companion forwards argv to the wrapper and passes stdout + exit through", () => {
  // No subcommand -> the wrapper prints exactly one failure (usage-error)
  // envelope to stdout and exits non-zero. This exercises resolution +
  // python3 exec + stdout passthrough + exit passthrough without a live run.
  const result = spawnSync(process.execPath, [COMPANION], { encoding: "utf8" });
  assert.notEqual(result.status, 0, "usage error must forward a non-zero exit");
  const parsed = JSON.parse(result.stdout.trim());
  assert.equal(parsed.status, "failure");
  assert.equal(parsed.error.class, "usage-error");
});
