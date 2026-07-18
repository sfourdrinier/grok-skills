// plugin/scripts/tests/direct-parity.test.mjs
//
// Task 1.6: direct-mode job-surface parity + honest handoff/status refusal.
// Fake-wrapper only - unregistered mode exit 2 proves no wrapper spawn.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  DIRECT_NO_HANDOFF_MSG,
  DIRECT_RUN_ID_RE,
  resolveDirectTimeoutSeconds,
  runDirectGrok,
} from "../lib/direct-grok.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
import {
  createJob,
  storeJobStdout,
  updateJob,
} from "../lib/jobs.mjs";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

const DIRECT_ID = "direct-1234567890";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-parity-"));
}

test("[4] runDirectGrok redacts secrets in the direct-mode envelope", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-redact-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    const secret = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\nprintf '%s\\n' '{"result":"here is a token ${secret} end"}'\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(secret),
      `secret must be redacted from the direct envelope: ${res.envelopeText}`
    );
    assert.match(res.envelopeText, /redacted/i, "redaction marker expected");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("[4] direct fallback withholds error.message when redaction cannot run", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-failclosed-"));
  try {
    const secret = "sk-" + "SECRETSECRETSECRETSECRET0123456789";
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Emit the secret on STDERR and exit nonzero -> envelope.error.message = secret.
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nprintf '%s\\n' 'token ${secret} here' 1>&2\nexit 3\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "/nonexistent-python-interpreter", // redaction cannot run -> fail closed
    });
    assert.doesNotMatch(
      res.envelopeText,
      new RegExp(secret),
      `secret must be withheld from the fail-closed envelope: ${res.envelopeText}`
    );
    assert.match(res.envelopeText, /withheld/i);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("resolveDirectTimeoutSeconds: per-mode defaults, override, junk, clamp", () => {
  assert.equal(resolveDirectTimeoutSeconds([], "code"), 3600);
  assert.equal(resolveDirectTimeoutSeconds([], "verify"), 1800);
  assert.equal(resolveDirectTimeoutSeconds([], "reason"), 900);
  assert.equal(resolveDirectTimeoutSeconds([], "review"), 900);
  assert.equal(resolveDirectTimeoutSeconds([], "adversarial-review"), 900);
  assert.equal(resolveDirectTimeoutSeconds([], "unknown-mode"), 900);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "120"], "code"), 120);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout=45"], "code"), 45);
  // junk / non-positive -> per-mode default
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "0"], "code"), 3600);
  // "--timeout -5" hits the flag-rejection branch (value starts with "-");
  // the equals form exercises the parsed n<=0 branch directly.
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "-5"], "code"), 3600);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout=-5"], "code"), 3600);
  assert.equal(resolveDirectTimeoutSeconds(["--timeout", "abc"], "verify"), 1800);
  // clamped to the 7-day ceiling
  assert.equal(
    resolveDirectTimeoutSeconds(["--timeout", String(99 * 24 * 3600)], "code"),
    7 * 24 * 3600
  );
});

test("runDirectGrok stages the prompt file with private 0600 perms", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-promptperm-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Echo the mode of the --prompt-file so we can assert it end to end. BSD stat
    // (macOS) uses -f %Lp; GNU stat (Linux) uses -c %a.
    fs.writeFileSync(
      fakeGrok,
      `#!/bin/sh\npf=""\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "--prompt-file" ]; then pf="$2"; fi\n  shift\ndone\nm=$(stat -f '%Lp' "$pf" 2>/dev/null || stat -c '%a' "$pf")\nprintf '{"result":"mode=%s"}\\n' "$m"\n`
    );
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "secret prompt body"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.match(res.envelopeText, /mode=600/, `prompt file must be 0600: ${res.envelopeText}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runDirectGrok honors --timeout and classifies a hung CLI as timed out", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-direct-timeout-"));
  try {
    const fakeGrok = path.join(dir, "fake-grok.sh");
    // Sleeps far longer than the 1s --timeout; the spawn must kill it.
    fs.writeFileSync(fakeGrok, `#!/bin/sh\nsleep 30\nprintf '{"result":"done"}\\n'\n`);
    fs.chmodSync(fakeGrok, 0o755);
    const scriptsDir = path.resolve(SCRIPT_DIR, "..", "..", "wrapper", "scripts");
    const res = runDirectGrok({
      mode: "code",
      args: ["--target", dir, "--base", "HEAD", "--task", "x", "--timeout", "1"],
      cwd: dir,
      env: { ...process.env, GROK_AGENT_BINARY: fakeGrok },
      scriptsDir,
      python: "python3",
    });
    assert.equal(res.code, 1, `timed-out run must exit nonzero: ${res.envelopeText}`);
    const env = JSON.parse(res.envelopeText);
    assert.equal(env.status, "failure");
    assert.match(env.error?.message || "", /timeout/i);
    assert.equal(env.error?.detail?.timedOut, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("result resolves direct-<timestamp> runId via the job index", () => {
  assert.ok(DIRECT_RUN_ID_RE.test(DIRECT_ID));
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  const job = createJob(cwd, { kind: "review", mode: "review", runMode: "direct" }, envBase);
  updateJob(cwd, job.id, { runId: DIRECT_ID, status: "success" }, envBase);
  const payload = JSON.stringify({
    status: "success",
    mode: "review",
    runId: DIRECT_ID,
    response: { text: "direct-job-output" },
  });
  storeJobStdout(cwd, job.id, `${payload}\n`, envBase);

  // Empty fake wrapper: result must not need the wrapper at all.
  const { env: fakeEnv, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["result", DIRECT_ID], {
      cwd,
      env: { ...fakeEnv, CLAUDE_PLUGIN_DATA: pluginData },
    });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.match(res.stdout, /direct-job-output/);
    assert.match(res.stdout, new RegExp(DIRECT_ID));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("handoff --run-id direct-* refuses before wrapper spawn", () => {
  const cwd = tempCwd();
  // Empty responses: if wrapper were spawned, unregistered mode exits 2.
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["handoff", "--run-id", DIRECT_ID], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") },
    });
    assert.equal(res.code, 1, `expected exit 1; got ${res.code}; stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "handoff direct-id refuse must not spawn the wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("status --run-id direct-* refuses with the same shared message", () => {
  const cwd = tempCwd();
  const { env, cleanup } = makeFakeWrapper({});
  try {
    const res = runCompanion(["status", "--run-id", DIRECT_ID], {
      cwd,
      env: { ...env, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") },
    });
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.ok(
      res.stderr.includes(DIRECT_NO_HANDOFF_MSG),
      `stderr must contain DIRECT_NO_HANDOFF_MSG; got: ${res.stderr}`
    );
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "status direct-id refuse must not spawn the wrapper"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("DIRECT_NO_HANDOFF_MSG is the single shared refusal string", () => {
  assert.equal(typeof DIRECT_NO_HANDOFF_MSG, "string");
  assert.match(DIRECT_NO_HANDOFF_MSG, /direct-mode runs have no hardened run state/);
  assert.match(DIRECT_NO_HANDOFF_MSG, /setup --run-mode hardened/);
});
