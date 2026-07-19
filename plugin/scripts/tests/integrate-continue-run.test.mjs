// plugin/scripts/tests/integrate-continue-run.test.mjs
//
// continue-run + integration auto/review/direct apply workspace resolution.
// Core auto apply spine: integrate-auto.test.mjs

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { makeFakeWrapper, readCalls, runCompanion } from "./helpers/fake-wrapper.mjs";
import {
  RUN_ID,
  codeEnvelope,
  handoffEnvelope,
  git,
  initTargetRepo,
  capturePatchAndRestore,
  stagePatch,
  companionEnv,
} from "./helpers/integrate-auto-fixtures.mjs";

// End-to-end continue-run + auto: prior run.json target/repository is the apply
// workspace (--target is forbidden on continue-run). Ready patch must land there.
// review retains (no auto apply); direct maps wrapper worktree lineage without live apply.
test("continue-run auto applies ready patch to prior run.json target/repository", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-auto-"));
  const companionCwd = path.join(root, "companion-cwd");
  fs.mkdirSync(companionCwd, { recursive: true });
  const targetRepo = initTargetRepo(path.join(root, "prior-target"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(targetRepo);
  stagePatch(xdg, RUN_ID, patchBody);
  // Prior run metadata: apply must use this repository, not companionCwd.
  const runDir = path.join(xdg, "grok-skills", "runs", RUN_ID);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "run.json"),
    JSON.stringify({
      runId: RUN_ID,
      repository: targetRepo,
      targetWorkspace: targetRepo,
    }) + "\n"
  );

  const before = fs.readFileSync(path.join(targetRepo, "foo.txt"), "utf8");
  assert.equal(before, "hello\n");
  // companionCwd must remain clean (prove apply does not target it).
  fs.writeFileSync(path.join(companionCwd, "sentinel.txt"), "untouched\n");

  const callsPath = path.join(root, "calls.log");
  const ready = handoffEnvelope(true);
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: { stdout: `${ready}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--continue-run",
        RUN_ID,
        "--base",
        "HEAD",
        "--task",
        "continue apply",
      ],
      { cwd: companionCwd, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 0, `stderr: ${res.stderr}\nstdout: ${res.stdout}`);
    assert.equal(
      fs.readFileSync(path.join(targetRepo, "foo.txt"), "utf8"),
      "hello world\n",
      "prior-run repository must receive the applied patch"
    );
    assert.equal(
      fs.readFileSync(path.join(companionCwd, "sentinel.txt"), "utf8"),
      "untouched\n",
      "companion cwd must not be the apply target"
    );
    assert.match(res.stderr, /APPLIED|applied/i);
    assert.deepEqual(readCalls(callsPath), ["code", "handoff", "handoff"]);
    const envLines = res.stdout.split("\n").filter((l) => l.trim().startsWith("{"));
    assert.equal(envLines.length, 1, `one envelope; got: ${res.stdout}`);
    const finalEnv = JSON.parse(envLines[0]);
    assert.equal(finalEnv.status, "success");
    assert.equal(finalEnv.mode, "code");
    assert.equal(finalEnv.response?.integration?.applied, true);
    assert.equal(finalEnv.response?.integration?.outcome, "applied");
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("continue-run review retains (no auto apply to prior target)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-review-"));
  const companionCwd = path.join(root, "companion-cwd");
  fs.mkdirSync(companionCwd, { recursive: true });
  const targetRepo = initTargetRepo(path.join(root, "prior-target"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(targetRepo);
  stagePatch(xdg, RUN_ID, patchBody);
  const runDir = path.join(xdg, "grok-skills", "runs", RUN_ID);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "run.json"),
    JSON.stringify({ repository: targetRepo, targetWorkspace: targetRepo }) + "\n"
  );
  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "review",
        "--continue-run",
        RUN_ID,
        "--task",
        "retain only",
      ],
      { cwd: companionCwd, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    // review: no handoff+apply path; target unchanged.
    assert.equal(fs.readFileSync(path.join(targetRepo, "foo.txt"), "utf8"), "hello\n");
    assert.deepEqual(readCalls(callsPath), ["code"]);
    assert.ok(!/APPLIED runId=/i.test(res.stderr), "review must not auto-apply");
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("continue-run auto: relative targetWorkspace resolves apply to original repo from foreign cwd", () => {
  // run.json may record package-relative targetWorkspace ("pkg"). Continuation
  // from outside the original repo must still auto-apply into originalRepo/pkg.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-rel-auto-"));
  const foreignCwd = path.join(root, "outside");
  fs.mkdirSync(foreignCwd, { recursive: true });
  // Decoy package path under foreign cwd (wrong target if resolved against cwd).
  fs.mkdirSync(path.join(foreignCwd, "pkg"), { recursive: true });
  fs.writeFileSync(path.join(foreignCwd, "pkg", "foo.txt"), "wrong\n");

  const originalRepo = path.join(root, "original-repo");
  fs.mkdirSync(path.join(originalRepo, "pkg"), { recursive: true });
  git(originalRepo, ["init"]);
  git(originalRepo, ["config", "user.email", "test@example.com"]);
  git(originalRepo, ["config", "user.name", "Test"]);
  fs.writeFileSync(path.join(originalRepo, "pkg", "foo.txt"), "hello\n");
  git(originalRepo, ["add", "pkg/foo.txt"]);
  git(originalRepo, ["commit", "-m", "init"]);

  const file = path.join(originalRepo, "pkg", "foo.txt");
  const original = fs.readFileSync(file, "utf8");
  fs.writeFileSync(file, "hello world\n");
  const diff = spawnSync("git", ["diff", "--binary", "HEAD"], {
    cwd: originalRepo,
    encoding: "utf8",
  });
  assert.equal(diff.status, 0, diff.stderr);
  assert.ok(diff.stdout.includes("pkg/foo.txt"), "patch must mention pkg/foo.txt");
  fs.writeFileSync(file, original);

  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, diff.stdout);
  const runDir = path.join(xdg, "grok-skills", "runs", RUN_ID);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "run.json"),
    JSON.stringify({
      runId: RUN_ID,
      repository: originalRepo,
      targetWorkspace: "pkg",
    }) + "\n"
  );

  const callsPath = path.join(root, "calls.log");
  const ready = handoffEnvelope(true);
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: { stdout: `${ready}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--continue-run",
        RUN_ID,
        "--task",
        "continue apply relative",
      ],
      { cwd: foreignCwd, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 0, `stderr: ${res.stderr}\nstdout: ${res.stdout}`);
    assert.equal(
      fs.readFileSync(path.join(originalRepo, "pkg", "foo.txt"), "utf8"),
      "hello world\n",
      "original repo/pkg must receive the applied patch"
    );
    assert.equal(
      fs.readFileSync(path.join(foreignCwd, "pkg", "foo.txt"), "utf8"),
      "wrong\n",
      "foreign cwd decoy must not be the apply target"
    );
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("continue-run direct maps wrapper worktree lineage without live apply", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-direct-"));
  const companionCwd = path.join(root, "companion-cwd");
  fs.mkdirSync(companionCwd, { recursive: true });
  const targetRepo = initTargetRepo(path.join(root, "prior-target"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(targetRepo);
  stagePatch(xdg, RUN_ID, patchBody);
  const runDir = path.join(xdg, "grok-skills", "runs", RUN_ID);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "run.json"),
    JSON.stringify({ repository: targetRepo, targetWorkspace: targetRepo }) + "\n"
  );
  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "direct",
        "--continue-run",
        RUN_ID,
        "--task",
        "no live apply",
      ],
      { cwd: companionCwd, env: companionEnv(env, root, xdg, callsPath) }
    );
    // Consent-exempt continue-run; wrapper lineage (worktree), not live apply.
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.equal(fs.readFileSync(path.join(targetRepo, "foo.txt"), "utf8"), "hello\n");
    assert.deepEqual(readCalls(callsPath), ["code"], "must use wrapper, not direct live apply");
    assert.ok(!/APPLIED runId=/i.test(res.stderr));
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
