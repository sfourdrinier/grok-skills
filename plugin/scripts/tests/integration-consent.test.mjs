// plugin/scripts/tests/integration-consent.test.mjs
//
// Task 7.2: one-time consent gate for --integration direct (companion).
// Orthogonal to runMode (hardened|direct security posture).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  getIntegrationConsent,
  getIntegrationMode,
  getRunMode,
  setIntegrationMode,
  setRunMode,
} from "../lib/jobs.mjs";
import {
  companionIsolation,
  makeFakeWrapper,
  readCalls,
  runCompanion,
} from "./helpers/fake-wrapper.mjs";

const RID = "20260716T120000Z-abc123";

function codeEnvelope() {
  return JSON.stringify({
    schemaVersion: 1,
    mode: "code",
    status: "success",
    runId: RID,
    response: { text: "ok" },
  });
}

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-int-consent-"));
}

const TRUST_SUMMARY_RE =
  /Direct integration lets Grok edit files in THIS working tree directly/i;
const SETUP_CMD_RE = /setup --integration direct/;

test("RED: default code (no consent) refuses with trust summary; wrapper not spawned", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      { cwd, env: { ...env, FAKE_WRAPPER_CALLS: callsPath } }
    );
    assert.notEqual(res.code, 0, `expected non-zero refuse; got ${res.code}`);
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    assert.match(res.stderr, SETUP_CMD_RE);
    assert.match(res.stderr, /--integration worktree|--integration review/);
    // Wrapper must not have been spawned (no code call logged).
    assert.deepEqual(readCalls(callsPath), []);
    assert.ok(
      !res.stderr.includes("[fake-wrapper] unregistered mode"),
      "refuse must happen before wrapper spawn (no unregistered-mode probe)"
    );
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("RED: explicit --integration direct without consent refuses", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      { cwd, env: { ...env, FAKE_WRAPPER_CALLS: callsPath } }
    );
    assert.notEqual(res.code, 0);
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    assert.match(res.stderr, SETUP_CMD_RE);
    assert.deepEqual(readCalls(callsPath), []);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("setup --run-mode direct is forwarded to cmdSetup and persists the posture", () => {
  // Regression: the setup dispatch branch stripped --run-mode without reattaching
  // it, so `/grok:setup --run-mode direct` silently left the posture unchanged.
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };
  assert.equal(getRunMode(cwd, envBase), "hardened", "starts hardened");
  const intBefore = getIntegrationMode(cwd, envBase);
  const res = runCompanion(["setup", "--run-mode", "direct", "--skip-codex-agents"], {
    cwd,
    env: envBase,
  });
  assert.ok(res.code === 0 || res.code === 1, res.stderr);
  assert.equal(getRunMode(cwd, envBase), "direct", "run mode must switch to direct");
  // Integration mode is orthogonal and must NOT be set by --run-mode.
  assert.equal(
    getIntegrationMode(cwd, envBase),
    intBefore,
    "--run-mode must not change integration mode"
  );
  const setBack = runCompanion(["setup", "--run-mode", "hardened", "--skip-codex-agents"], {
    cwd,
    env: envBase,
  });
  assert.ok(setBack.code === 0 || setBack.code === 1, setBack.stderr);
  assert.equal(getRunMode(cwd, envBase), "hardened", "run mode must switch back");
});

test("after setup --integration direct, code proceeds with explicit --integration direct", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };

  // setup --integration direct records mode + consent
  const setupRes = runCompanion(
    ["setup", "--integration", "direct", "--skip-codex-agents"],
    { cwd, env: envBase }
  );
  // setup may exit 1 without grok CLI; prefs must still apply
  assert.ok(setupRes.code === 0 || setupRes.code === 1, setupRes.stderr);
  assert.equal(getIntegrationMode(cwd, envBase), "direct");
  assert.equal(getIntegrationConsent(cwd, envBase), true);
  // runMode prefs untouched
  assert.equal(getRunMode(cwd, envBase), "hardened");

  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      {
        cwd,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsPath },
      }
    );
    assert.equal(res.code, 0, res.stderr);
    assert.deepEqual(readCalls(callsPath), ["code"]);
    const envelope = JSON.parse(res.stdout.trim());
    const argv = envelope.argv || [];
    const idx = argv.indexOf("--integration");
    assert.ok(idx >= 0, `expected --integration in argv: ${JSON.stringify(argv)}`);
    assert.equal(argv[idx + 1], "direct");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("code --integration worktree needs no consent; wrapper gets worktree", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "worktree",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      { cwd, env: { ...env, FAKE_WRAPPER_CALLS: callsPath } }
    );
    assert.equal(res.code, 0, res.stderr);
    assert.deepEqual(readCalls(callsPath), ["code"]);
    const envelope = JSON.parse(res.stdout.trim());
    const argv = envelope.argv || [];
    const idx = argv.indexOf("--integration");
    assert.ok(idx >= 0, `expected --integration in argv: ${JSON.stringify(argv)}`);
    assert.equal(argv[idx + 1], "worktree");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("env CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE=direct alone does not satisfy consent", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      {
        cwd,
        env: {
          ...env,
          FAKE_WRAPPER_CALLS: callsPath,
          CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "direct",
        },
      }
    );
    assert.notEqual(res.code, 0);
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    assert.deepEqual(readCalls(callsPath), []);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("integration prefs leave runMode hardened/direct untouched", () => {
  const cwd = tempCwd();
  const env = { CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  assert.equal(getRunMode(cwd, env), "hardened");
  setRunMode(cwd, "direct", env);
  assert.equal(getRunMode(cwd, env), "direct");

  setIntegrationMode(cwd, "worktree", env);
  assert.equal(getIntegrationMode(cwd, env), "worktree");
  assert.equal(getIntegrationConsent(cwd, env), false);
  assert.equal(getRunMode(cwd, env), "direct", "runMode must stay direct");

  setIntegrationMode(cwd, "direct", env);
  assert.equal(getIntegrationMode(cwd, env), "direct");
  assert.equal(getIntegrationConsent(cwd, env), true);
  assert.equal(getRunMode(cwd, env), "direct", "runMode must stay direct after consent");

  setRunMode(cwd, "hardened", env);
  assert.equal(getRunMode(cwd, env), "hardened");
  assert.equal(getIntegrationMode(cwd, env), "direct");
  assert.equal(getIntegrationConsent(cwd, env), true);
  fs.rmSync(cwd, { recursive: true, force: true });
});

test("getIntegrationMode precedence: setup > CLAUDE_PLUGIN_OPTION_* > default", () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");

  assert.equal(
    getIntegrationMode(cwd, { CLAUDE_PLUGIN_DATA: pluginData }),
    "direct"
  );

  assert.equal(
    getIntegrationMode(cwd, {
      CLAUDE_PLUGIN_DATA: pluginData,
      CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "worktree",
    }),
    "worktree"
  );

  const env = {
    CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata2"),
    CLAUDE_PLUGIN_OPTION_INTEGRATIONMODE: "worktree",
  };
  setIntegrationMode(cwd, "review", env);
  assert.equal(getIntegrationMode(cwd, env), "review");
  // Consent only for direct
  assert.equal(getIntegrationConsent(cwd, env), false);
  fs.rmSync(cwd, { recursive: true, force: true });
});

test("companionIsolation fresh workspace has no consent", () => {
  const iso = companionIsolation({});
  try {
    assert.equal(getIntegrationConsent(iso.cwd, iso.env), false);
    assert.equal(getIntegrationMode(iso.cwd, iso.env), "direct");
  } finally {
    iso.cleanup();
  }
});

// --- Task 7.2b: consent keys on TARGET repo, not companion cwd ---

function initGitRepo(dir) {
  fs.mkdirSync(dir, { recursive: true });
  const r = spawnSync("git", ["init"], { cwd: dir, encoding: "utf8" });
  assert.equal(r.status, 0, `git init failed: ${r.stderr}`);
  // Distinct content so two repos never share a path accidentally.
  fs.writeFileSync(path.join(dir, "README.md"), `repo ${path.basename(dir)}\n`);
  spawnSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
  spawnSync("git", ["config", "user.name", "Test"], { cwd: dir });
  spawnSync("git", ["add", "README.md"], { cwd: dir });
  spawnSync("git", ["commit", "-m", "init", "--allow-empty"], { cwd: dir });
  return dir;
}

test("CROSS-REPO RED: consent for workspace A does not authorize direct against B", () => {
  // LIVE FINDING: consent recorded while companion cwd = A authorized
  // code --target B --integration direct. Consent must key on TARGET.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-cross-repo-"));
  const repoA = initGitRepo(path.join(root, "repo-a"));
  const repoB = initGitRepo(path.join(root, "repo-b"));
  const pluginData = path.join(root, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };

  // Consent only for A (setup while cwd = A, no --target => A).
  const setupRes = runCompanion(
    ["setup", "--integration", "direct", "--skip-codex-agents"],
    { cwd: repoA, env: envBase }
  );
  assert.ok(setupRes.code === 0 || setupRes.code === 1, setupRes.stderr);
  assert.equal(getIntegrationConsent(repoA, envBase), true, "A must have consent");
  assert.equal(getIntegrationConsent(repoB, envBase), false, "B must NOT have consent");

  const callsPath = path.join(root, "calls-cross.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    // cwd stays A; --target is B; no consent for B => MUST refuse.
    const res = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        repoB,
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      {
        cwd: repoA,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsPath },
      }
    );
    assert.notEqual(
      res.code,
      0,
      `cross-repo direct without B consent must refuse; got code=${res.code} stderr=${res.stderr}`
    );
    assert.match(res.stderr, TRUST_SUMMARY_RE);
    // Accept command should name the target when target != cwd.
    assert.match(
      res.stderr,
      /setup --integration direct --target/,
      `refuse message must include setup --target for cross-repo; got: ${res.stderr}`
    );
    assert.deepEqual(
      readCalls(callsPath),
      [],
      "wrapper must not spawn when B has no consent"
    );
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("setup --integration direct --target B consents B only; C still refused", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-target-consent-"));
  const repoA = initGitRepo(path.join(root, "repo-a"));
  const repoB = initGitRepo(path.join(root, "repo-b"));
  const repoC = initGitRepo(path.join(root, "repo-c"));
  const pluginData = path.join(root, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };

  // From cwd A, consent for B via --target.
  const setupRes = runCompanion(
    [
      "setup",
      "--integration",
      "direct",
      "--target",
      repoB,
      "--skip-codex-agents",
    ],
    { cwd: repoA, env: envBase }
  );
  assert.ok(setupRes.code === 0 || setupRes.code === 1, setupRes.stderr);
  assert.equal(getIntegrationConsent(repoA, envBase), false, "A must not gain consent");
  assert.equal(getIntegrationConsent(repoB, envBase), true, "B must have consent");
  assert.equal(getIntegrationConsent(repoC, envBase), false, "C must not have consent");

  const callsB = path.join(root, "calls-b.log");
  const callsC = path.join(root, "calls-c.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    // Direct against B proceeds.
    const resB = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        repoB,
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      {
        cwd: repoA,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsB },
      }
    );
    assert.equal(resB.code, 0, resB.stderr);
    assert.deepEqual(readCalls(callsB), ["code"]);

    // Direct against C still refused.
    const resC = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        repoC,
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      {
        cwd: repoA,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsC },
      }
    );
    assert.notEqual(resC.code, 0, "C without consent must refuse");
    assert.match(resC.stderr, TRUST_SUMMARY_RE);
    assert.deepEqual(readCalls(callsC), []);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("normal case: cwd==target with consent proceeds (unchanged)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-same-target-"));
  const repo = initGitRepo(path.join(root, "repo"));
  const pluginData = path.join(root, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };

  const setupRes = runCompanion(
    ["setup", "--integration", "direct", "--skip-codex-agents"],
    { cwd: repo, env: envBase }
  );
  assert.ok(setupRes.code === 0 || setupRes.code === 1, setupRes.stderr);
  assert.equal(getIntegrationConsent(repo, envBase), true);

  const callsPath = path.join(root, "calls-same.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    const res = runCompanion(
      ["code", "--target", ".", "--base", "HEAD", "--task", "x"],
      {
        cwd: repo,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsPath },
      }
    );
    assert.equal(res.code, 0, res.stderr);
    assert.deepEqual(readCalls(callsPath), ["code"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("non-git target: consent keyed on absolute target dir (per-target)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-nongit-"));
  const dirA = path.join(root, "plain-a");
  const dirB = path.join(root, "plain-b");
  fs.mkdirSync(dirA, { recursive: true });
  fs.mkdirSync(dirB, { recursive: true });
  const pluginData = path.join(root, "pdata");
  const envBase = { CLAUDE_PLUGIN_DATA: pluginData };

  // Consent for non-git dir A only.
  const setupRes = runCompanion(
    [
      "setup",
      "--integration",
      "direct",
      "--target",
      dirA,
      "--skip-codex-agents",
    ],
    { cwd: root, env: envBase }
  );
  assert.ok(setupRes.code === 0 || setupRes.code === 1, setupRes.stderr);
  assert.equal(getIntegrationConsent(dirA, envBase), true);
  assert.equal(getIntegrationConsent(dirB, envBase), false);

  const callsA = path.join(root, "calls-na.log");
  const callsB = path.join(root, "calls-nb.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { echoTask: true },
  });
  try {
    const resA = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        dirA,
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      {
        cwd: root,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsA },
      }
    );
    assert.equal(resA.code, 0, resA.stderr);
    assert.deepEqual(readCalls(callsA), ["code"]);

    const resB = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--target",
        dirB,
        "--base",
        "HEAD",
        "--task",
        "x",
      ],
      {
        cwd: root,
        env: { ...env, ...envBase, FAKE_WRAPPER_CALLS: callsB },
      }
    );
    assert.notEqual(resB.code, 0, "different non-git dir must not inherit consent");
    assert.match(resB.stderr, TRUST_SUMMARY_RE);
    assert.deepEqual(readCalls(callsB), []);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
