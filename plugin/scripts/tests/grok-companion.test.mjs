// plugin/scripts/tests/grok-companion.test.mjs
//
// Verifies the companion resolves the wrapper and forwards argv + stdout + exit
// code unchanged. Run with: node --test plugin/scripts/tests/

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  candidateWrapperPaths,
  resolveWrapperPath,
  wrapperNotFoundMessage
} from "../lib/wrapper.mjs";
import { wrapperChildEnv, NOTIFY_ELIGIBLE_MODES, shouldNotify } from "../lib/notify.mjs";
import { RUN_ID_RE } from "../progress-relay.mjs";

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

test("wrapperChildEnv strips execution context for wrapper spawns", () => {
  const scrubbed = wrapperChildEnv({
    GROK_COMPANION_EXECUTION_CONTEXT: "background",
    PATH: "/bin"
  });
  assert.equal(scrubbed.GROK_COMPANION_EXECUTION_CONTEXT, undefined);
  assert.equal(scrubbed.PATH, "/bin");
});

test("notify eligibility excludes status/setup/cleanup/jobs", () => {
  for (const mode of ["status", "setup", "cleanup", "preflight", "jobs", "result", "cancel"]) {
    assert.equal(NOTIFY_ELIGIBLE_MODES.has(mode), false, mode);
  }
  for (const mode of ["review", "reason", "code", "verify", "adversarial-review"]) {
    assert.equal(NOTIFY_ELIGIBLE_MODES.has(mode), true, mode);
  }
});

test("auto notify is background-only (companion policy)", () => {
  assert.equal(
    shouldNotify({ notificationMode: "auto", executionContext: "foreground" }).notify,
    false
  );
  assert.equal(
    shouldNotify({ notificationMode: "auto", executionContext: "background" }).notify,
    true
  );
});

test("run id shape used for notify is the same as progress-relay", () => {
  assert.equal(RUN_ID_RE.test("20260716T120000Z-deadbe"), true);
  assert.equal(RUN_ID_RE.test("../../../etc"), false);
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

function runSetup(args, envExtras = {}) {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-setup-"));
  const pdata = path.join(cwd, "pdata");
  const result = spawnSync(process.execPath, [COMPANION, "setup", ...args], {
    encoding: "utf8",
    cwd,
    env: {
      ...process.env,
      CLAUDE_PLUGIN_DATA: pdata,
      ...envExtras,
    },
  });
  return { result, cwd, pdata };
}

test("setup rejects invalid --notification-mode without changing prefs", async () => {
  const { getNotificationConfig } = await import("../lib/jobs.mjs");
  // setup exits 1 when grok CLI is missing (CI) even if notify prefs apply.
  // Assert durable prefs + report text, not overall process status.
  const first = runSetup(["--notification-mode", "auto", "--skip-codex-agents"]);
  assert.ok(first.result.status === 0 || first.result.status === 1);
  assert.match(first.result.stdout + first.result.stderr, /notifications/i);
  assert.equal(
    getNotificationConfig(first.cwd, { CLAUDE_PLUGIN_DATA: first.pdata }).notificationMode,
    "auto"
  );

  const bad = spawnSync(
    process.execPath,
    [COMPANION, "setup", "--notification-mode", "telepathy", "--skip-codex-agents"],
    {
      encoding: "utf8",
      cwd: first.cwd,
      env: { ...process.env, CLAUDE_PLUGIN_DATA: first.pdata },
    }
  );
  const badOut = bad.stdout + bad.stderr;
  assert.match(badOut, /invalid mode/i);
  assert.match(badOut, /telepathy/);
  assert.equal(
    getNotificationConfig(first.cwd, { CLAUDE_PLUGIN_DATA: first.pdata }).notificationMode,
    "auto",
    "invalid mode must not clobber prior auto"
  );
});

test("setup redacts webhook URL query/userinfo from report", async () => {
  const { getNotificationConfig } = await import("../lib/jobs.mjs");
  const secretUrl =
    "https://user:token@hooks.example.com/notify?secret=super-secret-token";
  const { result, cwd, pdata } = runSetup([
    "--notification-mode",
    "webhook",
    "--notification-webhook-url",
    secretUrl,
    "--skip-codex-agents",
  ]);
  // Exit may be 1 without grok CLI on CI; redaction must still hold in report.
  assert.ok(result.status === 0 || result.status === 1);
  const out = result.stdout + result.stderr;
  assert.doesNotMatch(out, /super-secret-token/);
  assert.doesNotMatch(out, /user:token@/);
  assert.match(out, /hooks\.example\.com/);
  // Prefs still persist the full URL; redaction is display-only.
  assert.equal(
    getNotificationConfig(cwd, { CLAUDE_PLUGIN_DATA: pdata }).notificationWebhookUrl,
    secretUrl
  );
});

test("companion never maps terminal lifecycle to running (source contract)", async () => {
  const src = fs.readFileSync(COMPANION, "utf8");
  // Guard against re-introducing envelope status "running" as notify lifecycle.
  assert.equal(
    /lifecycle\s*=\s*["']running["']/.test(src),
    false,
    "maybeNotifyAfterTerminal must not set lifecycle to running"
  );
  assert.match(src, /safeRunIdForRunsDir|sanitizeRunId/);
  assert.match(src, /notifyMode/);
});
