// plugin/scripts/tests/integrate-auto.test.mjs
//
// Task 7.3: integration=auto - apply-on-verified-ready with apply-time revalidation.
// Real temp git target so apply is exercised on disk (fake-wrapper for code/handoff).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  companionIntegrationToWrapper,
  restForWrapperIntegration,
} from "../lib/implement.mjs";
import { applyVerifiedPatch, locateImplementationPatch } from "../lib/integrate.mjs";
import { makeFakeWrapper, readCalls, runCompanion } from "./helpers/fake-wrapper.mjs";

const RUN_ID = "20260717T120000Z-a1b2c3";

function codeEnvelope(overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: "success",
    mode: "code",
    runId: RUN_ID,
    response: { text: "code-done" },
    ...overrides,
  });
}

function handoffEnvelope(ready, overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: ready ? "success" : "failure",
    mode: "handoff",
    runId: RUN_ID,
    response: {
      integration: {
        ready,
        blockers: ready ? [] : [{ kind: "handoff-unavailable" }],
      },
    },
    ...overrides,
  });
}

function git(cwd, args) {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(r.status, 0, `git ${args.join(" ")} failed: ${r.stderr}`);
  return r;
}

function initTargetRepo(dir) {
  fs.mkdirSync(dir, { recursive: true });
  git(dir, ["init"]);
  git(dir, ["config", "user.email", "test@example.com"]);
  git(dir, ["config", "user.name", "Test"]);
  fs.writeFileSync(path.join(dir, "foo.txt"), "hello\n");
  git(dir, ["add", "foo.txt"]);
  git(dir, ["commit", "-m", "init"]);
  return dir;
}

/** Modify foo.txt, capture binary diff vs HEAD, restore original so apply can land. */
function capturePatchAndRestore(repo) {
  const file = path.join(repo, "foo.txt");
  const original = fs.readFileSync(file, "utf8");
  fs.writeFileSync(file, "hello world\n");
  const diff = spawnSync("git", ["diff", "--binary", "HEAD"], {
    cwd: repo,
    encoding: "utf8",
  });
  assert.equal(diff.status, 0, diff.stderr);
  assert.ok(diff.stdout.includes("foo.txt"), "patch must mention foo.txt");
  fs.writeFileSync(file, original);
  return diff.stdout;
}

function stagePatch(xdgStateHome, runId, patchBody) {
  const art = path.join(
    xdgStateHome,
    "grok-skills",
    "runs",
    runId,
    "artifacts"
  );
  fs.mkdirSync(art, { recursive: true });
  const patchPath = path.join(art, "implementation.patch");
  fs.writeFileSync(patchPath, patchBody);
  return patchPath;
}

function companionEnv(env, root, xdg, callsPath) {
  return {
    ...env,
    XDG_STATE_HOME: xdg,
    CLAUDE_PLUGIN_DATA: path.join(root, "pdata"),
    GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
    ...(callsPath ? { FAKE_WRAPPER_CALLS: callsPath } : {}),
  };
}

test("unit: companionIntegrationToWrapper maps auto/review to worktree", () => {
  assert.equal(companionIntegrationToWrapper("direct"), "direct");
  assert.equal(companionIntegrationToWrapper("worktree"), "worktree");
  assert.equal(companionIntegrationToWrapper("auto"), "worktree");
  assert.equal(companionIntegrationToWrapper("review"), "worktree");
});

test("unit: restForWrapperIntegration rewrites auto to worktree", () => {
  assert.deepEqual(
    restForWrapperIntegration(["--integration", "auto", "--target", ".", "--task", "x"]),
    ["--target", ".", "--task", "x", "--integration", "worktree"]
  );
  assert.deepEqual(
    restForWrapperIntegration(["--integration", "review", "--target", "r"]),
    ["--target", "r", "--integration", "worktree"]
  );
});

test("unit: locateImplementationPatch resolves under XDG state root", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-locate-patch-"));
  const xdg = path.join(root, "xdg");
  try {
    const p = stagePatch(xdg, RUN_ID, "diff --git a/x b/x\n");
    const found = locateImplementationPatch(RUN_ID, { XDG_STATE_HOME: xdg });
    assert.equal(found, p);
    assert.equal(
      locateImplementationPatch("20990101T000000Z-dead00", { XDG_STATE_HOME: xdg }),
      null
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto happy path: applies ready patch to real temp target; exit 0", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-happy-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);
  const before = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
  assert.equal(before, "hello\n");

  const callsPath = path.join(root, "calls.log");
  const ready = handoffEnvelope(true);
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    // First handoff (post-code) + second (apply-time revalidation)
    handoff: { stdout: `${ready}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "apply me",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 0, `stderr: ${res.stderr}\nstdout: ${res.stdout}`);
    const after = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
    assert.equal(after, "hello world\n", "target file must change on disk");
    assert.match(res.stderr, /APPLIED|applied/i);
    const calls = readCalls(callsPath);
    assert.deepEqual(calls, ["code", "handoff", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto not-ready: no apply; target unchanged; exit 1", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-notready-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);

  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: { stdout: `${handoffEnvelope(false)}\n`, exitCode: 1 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "nope",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "target must be unchanged when not ready"
    );
    assert.deepEqual(readCalls(callsPath), ["code", "handoff"]);
    assert.ok(
      !/APPLIED runId=/i.test(res.stderr),
      "must not claim applied when not ready"
    );
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto apply-time revalidation: tree mutation blocks half-apply; exit 1", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-reval-mut-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);
  const foo = path.join(repo, "foo.txt");

  const callsPath = path.join(root, "calls.log");
  const ready = `${handoffEnvelope(true)}\n`;
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    // 2nd handoff mutates target so git apply --check fails
    handoff: [
      { stdout: ready, exitCode: 0 },
      {
        stdout: ready,
        exitCode: 0,
        mutate: { path: foo, content: "diverged base content\n" },
      },
    ],
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "mutate race",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected blocked exit 1; stderr: ${res.stderr}`);
    // Mutate ran; apply must NOT have partially applied "hello world"
    const content = fs.readFileSync(foo, "utf8");
    assert.notEqual(content, "hello world\n", "must not half-apply the patch");
    assert.equal(
      content,
      "diverged base content\n",
      "target stays at the mutated pre-apply content (no apply)"
    );
    assert.match(res.stderr, /BLOCKED|apply --check|PARTIAL|blocked/i);
    assert.ok(!/APPLIED runId=/i.test(res.stderr), "must not claim ready-applied");
    assert.deepEqual(readCalls(callsPath), ["code", "handoff", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto dirty-overlap: blocks apply when a patch path is already dirty", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-dirty-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatchAndRestore(repo));
  // Operator is actively editing the same file the patch touches.
  fs.appendFileSync(path.join(repo, "foo.txt"), "operator work in progress\n");
  const before = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
  try {
    const res = applyVerifiedPatch({
      wrapper: "unused",
      runId: RUN_ID,
      targetRepo: repo,
      runHandoff: () => ({ code: 0, envelope: { response: { integration: { ready: true } } } }),
      stderrLine: () => {},
      env: { XDG_STATE_HOME: xdg },
    });
    assert.equal(res.ok, false);
    assert.equal(res.outcome, "blocked-dirty-overlap");
    assert.deepEqual(res.overlap, ["foo.txt"]);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      before,
      "target must be untouched when dirty-overlap blocks"
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto re-check flips to not-ready at apply time: no apply; exit 1", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-reval-flip-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);

  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: [
      { stdout: `${handoffEnvelope(true)}\n`, exitCode: 0 },
      { stdout: `${handoffEnvelope(false)}\n`, exitCode: 1 },
    ],
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "flip",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "target unchanged when revalidation flips not-ready"
    );
    assert.match(res.stderr, /revalidation|not ready|BLOCKED/i);
    assert.ok(!/APPLIED runId=/i.test(res.stderr));
    assert.deepEqual(readCalls(callsPath), ["code", "handoff", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
