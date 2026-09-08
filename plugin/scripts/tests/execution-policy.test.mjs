// plugin/scripts/tests/execution-policy.test.mjs
//
// F06: bounded serial work inherits the supplied workspace; explicit isolation
// prefs are not broadened; implement stays isolate-and-retain.

import assert from "node:assert/strict";
import { test } from "node:test";

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { applyExecutionPolicyToArgs, resolveExecutionPolicy } from "../lib/execution-policy.mjs";
import { projectTaskResult } from "../lib/task-result.mjs";
import { getJob } from "../lib/jobs.mjs";
import { flagValue } from "../lib/companion-args.mjs";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

test("code + direct inherits workspace and writes in place", () => {
  const policy = resolveExecutionPolicy({ skill: "code", integrationMode: "direct" });
  assert.equal(policy.workspace, "inherit");
  assert.equal(policy.transport, "oneshot");
  assert.equal(policy.application, "in-place");
  assert.equal(policy.validation, "targeted");
});

test("code + review/worktree stays isolated and retains the patch", () => {
  for (const mode of ["review", "worktree"]) {
    const policy = resolveExecutionPolicy({ skill: "code", integrationMode: mode });
    assert.equal(policy.workspace, "isolated", mode);
    assert.equal(policy.application, "retain-patch", mode);
  }
});

test("code + auto isolates then apply-when-verified", () => {
  const policy = resolveExecutionPolicy({ skill: "code", integrationMode: "auto" });
  assert.equal(policy.workspace, "isolated");
  assert.equal(policy.application, "apply-when-verified");
});

test("implement always isolates and does not live-apply", () => {
  const policy = resolveExecutionPolicy({
    skill: "implement",
    integrationMode: "direct",
  });
  assert.equal(policy.workspace, "isolated");
  assert.equal(policy.application, "retain-patch");
});

test("review/reason do not apply patches", () => {
  const policy = resolveExecutionPolicy({ skill: "review", integrationMode: "direct" });
  assert.equal(policy.workspace, "inherit");
  assert.equal(policy.application, "not-applicable");
});

test("continue-run reuses session transport", () => {
  const policy = resolveExecutionPolicy({
    skill: "code",
    integrationMode: "direct",
    continueRunId: "20260907T000000Z-abc123",
  });
  assert.equal(policy.transport, "session");
  assert.equal(policy.workspace, "inherit");
});

test("projectTaskResult is a projection of the envelope, not a second author", () => {
  const envelope = {
    schemaVersion: 1,
    status: "success",
    mode: "code",
    runId: "20260907T010203Z-deadbe",
    response: { text: "fixed the bug" },
  };
  const result = projectTaskResult(envelope);
  assert.equal(result.protocolVersion, 2);
  assert.equal(result.runId, envelope.runId);
  assert.equal(result.execution, "completed");
  assert.equal(result.summary, "fixed the bug");
  assert.equal(result.application, "in-place");
  assert.equal(result.workspace.placement, "inherit");
  assert.equal(result.verification.state, "not-run");
  const failed = projectTaskResult({ ...envelope, status: "failure" });
  assert.equal(failed.execution, "failed");
  const isolated = projectTaskResult({
    ...envelope,
    worktreePath: "/tmp/ws",
    response: { text: "fixed the bug", integration: { ready: true, applied: false } },
  });
  assert.equal(isolated.workspace.placement, "isolated");
  assert.equal(isolated.application, "patch-ready");
});

test("applyExecutionPolicyToArgs injects targeted validation for inherit code", () => {
  const { policy, args } = applyExecutionPolicyToArgs({
    skill: "code",
    args: ["--target", "."],
    integrationMode: "direct",
  });
  assert.equal(policy.workspace, "inherit");
  assert.equal(policy.validation, "targeted");
  assert.equal(flagValue(args, "--validation"), "targeted");
});

test("applyExecutionPolicyToArgs preserves an explicit --validation", () => {
  const { args } = applyExecutionPolicyToArgs({
    skill: "code",
    args: ["--validation", "full"],
    integrationMode: "direct",
  });
  assert.equal(flagValue(args, "--validation"), "full");
});

test("applyExecutionPolicyToArgs injects full validation for implement", () => {
  const { args } = applyExecutionPolicyToArgs({
    skill: "implement",
    args: ["--target", "."],
    integrationMode: "direct",
  });
  assert.equal(flagValue(args, "--validation"), "full");
});

test("companion dispatch calls applyExecutionPolicyToArgs and projectTaskResult", () => {
  const src = fs.readFileSync(path.join(ROOT, "scripts", "grok-companion.mjs"), "utf8");
  assert.match(src, /applyExecutionPolicyToArgs\(/);
  assert.match(src, /projectTaskResult\(/);
});

test("code inherit path passes --validation targeted to the wrapper", () => {
  const { env, cleanup } = makeFakeWrapper({ code: { echoTask: true } });
  try {
    const res = runCompanion(["code", "--task", "fix it"], { env });
    assert.equal(res.code, 0, res.stderr);
    const envelope = JSON.parse(String(res.stdout).trim());
    assert.ok(Array.isArray(envelope.argv), res.stdout);
    assert.equal(flagValue(envelope.argv, "--validation"), "targeted");
    assert.equal(flagValue(envelope.argv, "--integration"), "direct");
  } finally {
    cleanup();
  }
});

test("code --validation full is forwarded, not overwritten", () => {
  const { env, cleanup } = makeFakeWrapper({ code: { echoTask: true } });
  try {
    const res = runCompanion(["code", "--validation", "full", "--task", "fix it"], { env });
    assert.equal(res.code, 0, res.stderr);
    const envelope = JSON.parse(String(res.stdout).trim());
    assert.equal(flagValue(envelope.argv, "--validation"), "full");
  } finally {
    cleanup();
  }
});

test("finished code job stores compact TaskResult from the envelope", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-policy-job-"));
  const pluginData = path.join(cwd, ".grok-plugin-data");
  const { env, cleanup } = makeFakeWrapper({
    code: {
      stdout:
        JSON.stringify({
          schemaVersion: 1,
          status: "success",
          mode: "code",
          runId: "20260907T010203Z-deadbe",
          response: { text: "fixed the bug" },
        }) + "\n",
    },
  });
  env.CLAUDE_PLUGIN_DATA = pluginData;
  try {
    const res = runCompanion(["code", "--task", "fix it"], { env, cwd });
    assert.equal(res.code, 0, res.stderr);
    const match = String(res.stderr).match(/\[grok-job\] (\S+) started/);
    assert.ok(match, res.stderr);
    const job = getJob(cwd, match[1], env);
    assert.ok(job?.taskResult, JSON.stringify(job));
    assert.equal(job.taskResult.execution, "completed");
    assert.equal(job.taskResult.summary, "fixed the bug");
    assert.equal(job.taskResult.application, "in-place");
    assert.equal(job.taskResult.runId, "20260907T010203Z-deadbe");
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("engineer-coder default recipe is one-shot code, not peer start", () => {
  const md = fs.readFileSync(path.join(ROOT, "agents", "grok-engineer-coder.md"), "utf8");
  const toml = fs.readFileSync(
    path.join(ROOT, "codex-agents", "grok-engineer-coder.toml"),
    "utf8"
  );
  assert.match(md, /one-shot code in the (supplied|current)/i);
  assert.doesNotMatch(md, /## Default: ACP multi-turn peer/);
  assert.doesNotMatch(md, /Implementation call \(default: peer\)/);
  const codeIdx = md.search(/GROK_RUN code/);
  const peerIdx = md.search(/GROK_RUN peer start/);
  assert.ok(codeIdx >= 0, "md must show GROK_RUN code");
  assert.ok(peerIdx < 0 || codeIdx < peerIdx, "GROK_RUN code must precede peer start");
  assert.match(toml, /DEFAULT - one-shot code/);
  assert.match(toml, /OPT-IN multi-turn peer/);
});

test("in-place default recipe does not require code-mode handoff", () => {
  const md = fs.readFileSync(path.join(ROOT, "agents", "grok-engineer-coder.md"), "utf8");
  const toml = fs.readFileSync(
    path.join(ROOT, "codex-agents", "grok-engineer-coder.toml"),
    "utf8"
  );
  assert.doesNotMatch(md, /handoff is REQUIRED/i);
  assert.doesNotMatch(toml, /handoff is REQUIRED/i);
  assert.match(md, /In-place code/i);
  assert.match(toml, /In-place code/i);
  assert.match(md, /no patch handoff/i);
  assert.match(toml, /no patch handoff/i);
});

test("projectTaskResult treats hardened-direct as in-place with a real workspace path", () => {
  const result = projectTaskResult({
    schemaVersion: 1,
    status: "success",
    mode: "direct",
    runId: "20260907T010203Z-direct1",
    repository: "/tmp/host-repo",
    targetWorkspace: "",
    effectiveWorkingDirectory: "/tmp/host-repo",
    worktreePath: null,
    commands: [{ argv: ["pnpm", "test"], cwd: "pkg", exitStatus: 0, purpose: "test" }],
    response: { text: "edited live" },
  });
  assert.equal(result.application, "in-place");
  assert.equal(result.workspace.path, "/tmp/host-repo");
  assert.equal(result.workspace.placement, "inherit");
  assert.equal(result.verification.state, "passed");
});

test("projectTaskResult does not treat install-only or null exits as verified", () => {
  const installOnly = projectTaskResult({
    status: "success",
    mode: "direct",
    repository: "/tmp/repo",
    commands: [{ argv: ["pnpm", "install"], cwd: ".", exitStatus: 0, purpose: "install" }],
  });
  assert.equal(installOnly.verification.state, "not-run");
  const nullExit = projectTaskResult({
    status: "success",
    mode: "direct",
    repository: "/tmp/repo",
    commands: [{ argv: ["pnpm", "test"], cwd: ".", exitStatus: null, purpose: "test" }],
  });
  assert.equal(nullExit.verification.state, "not-run");
});

test("projectTaskResult preserves incomplete and cancelled envelopes", () => {
  const incomplete = projectTaskResult({
    status: "success",
    mode: "direct",
    repository: "/tmp/repo",
    incompleteStop: true,
    processExit: 1,
    commands: [{ argv: ["pnpm", "test"], cwd: ".", exitStatus: 0, purpose: "test" }],
    response: { text: "partial" },
  });
  assert.equal(incomplete.execution, "incomplete");
  assert.notEqual(incomplete.verification.state, "passed");
  const cancelled = projectTaskResult({
    status: "success",
    mode: "direct",
    repository: "/tmp/repo",
    stopReason: "cancelled",
    commands: [{ argv: ["pnpm", "test"], cwd: ".", exitStatus: 0, purpose: "test" }],
  });
  assert.equal(cancelled.execution, "cancelled");
});
