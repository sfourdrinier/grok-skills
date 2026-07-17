// plugin/scripts/tests/peer-acp.test.mjs
//
// Companion gate for ACP peer channel (Task 7.4: default on, GROK_DISABLE_ACP opt-out).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  makeFakeWrapper,
  runCompanion,
} from "./helpers/fake-wrapper.mjs";
import { runPeerStartBackground } from "../lib/peer-acp.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(SCRIPT_DIR, "..", "grok-companion.mjs");

/** Write a temp "wrapper" that prints one envelope line then exits. */
function fakeEnvelopeWrapper(envelopeLine) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "peer-start-"));
  const file = path.join(dir, "fake-wrapper.mjs");
  fs.writeFileSync(
    file,
    `process.stdout.write(${JSON.stringify(envelopeLine + "\n")});\n`
  );
  return { file, cleanup: () => fs.rmSync(dir, { recursive: true, force: true }) };
}

test("peer-start background: non-running first envelope exits nonzero", async () => {
  const { file, cleanup } = fakeEnvelopeWrapper(
    JSON.stringify({ status: "failure", mode: "peer-start", error_class: "auth-missing" })
  );
  try {
    const code = await runPeerStartBackground(process.execPath, file, [], {
      spawnFailedMessage: () => "",
      signalExit: 1,
      spawnFailedExit: 4,
    });
    assert.notEqual(code, 0, "a pre-resident failure envelope must not exit 0");
  } finally {
    cleanup();
  }
});

test("peer-start background: running envelope exits 0", async () => {
  const { file, cleanup } = fakeEnvelopeWrapper(
    JSON.stringify({ status: "running", mode: "peer-start", runId: "x" })
  );
  try {
    const code = await runPeerStartBackground(process.execPath, file, [], {
      spawnFailedMessage: () => "",
      signalExit: 1,
      spawnFailedExit: 4,
    });
    assert.equal(code, 0);
  } finally {
    cleanup();
  }
});

function runCompanionLegacy(args, env = {}) {
  return spawnSync(process.execPath, [COMPANION, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

test("peer modes work when GROK_EXPERIMENTAL_ACP is unset (default on)", () => {
  // Fake wrapper answers peer-start so we never hit the real ACP stack.
  const rid = "20260717T000000Z-aaaaaa";
  const { env, cleanup } = makeFakeWrapper({
    "peer-start": {
      stdout:
        JSON.stringify({
          schemaVersion: 1,
          mode: "peer-start",
          status: "running",
          runId: rid,
          response: { peer: { sessionId: "s", socketPath: "/tmp/p.sock" } },
        }) + "\n",
      exitCode: 0,
    },
  });
  try {
    const res = runCompanion(
      ["peer", "start", "--target", ".", "--base", "HEAD"],
      {
        env: {
          ...env,
          GROK_EXPERIMENTAL_ACP: "",
          GROK_DISABLE_ACP: "",
        },
      }
    );
    // Must not refuse with the old experimental gate.
    assert.ok(
      !(res.stderr || "").includes("GROK_EXPERIMENTAL_ACP=1"),
      `must not require experimental flag: ${res.stderr}`
    );
    assert.ok(
      !(res.stderr || "").includes("GROK_DISABLE_ACP"),
      `must not claim disabled: ${res.stderr}`
    );
  } finally {
    cleanup();
  }
});

test("peer modes refused when GROK_DISABLE_ACP=1", () => {
  for (const mode of ["peer", "peer-start", "peer-prompt", "peer-stop"]) {
    const args =
      mode === "peer"
        ? ["peer", "start", "--target", ".", "--base", "HEAD"]
        : [
            mode,
            ...(mode === "peer-start"
              ? ["--target", ".", "--base", "HEAD"]
              : ["--run-id", "20260717T000000Z-aaaaaa"]),
          ];
    const result = runCompanionLegacy(args, {
      GROK_DISABLE_ACP: "1",
      GROK_EXPERIMENTAL_ACP: "1",
    });
    assert.notEqual(result.status, 0, mode);
    assert.match(
      result.stderr || "",
      /GROK_DISABLE_ACP/,
      `stderr should mention opt-out for ${mode}: ${result.stderr}`
    );
    assert.match(
      result.stderr || "",
      /2026-07-17-acp-peer-channel-design/,
      `stderr should point at the spec for ${mode}`
    );
  }
});

test("peer modes refuse direct run-mode even when ACP is enabled", () => {
  const result = runCompanionLegacy(
    ["peer", "start", "--target", ".", "--base", "HEAD", "--run-mode", "direct"],
    { GROK_DISABLE_ACP: "", GROK_EXPERIMENTAL_ACP: "1" }
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr || "", /hardened/i);
});

test("peer-stop ready + integration=auto applies patch to target repo", () => {
  const rid = "20260717T120000Z-abc101";
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-apply-"));
  const target = path.join(tmp, "target");
  const state = path.join(tmp, "state");
  fs.mkdirSync(target, { recursive: true });
  // Mirror runstate: $XDG_STATE_HOME/grok-skills/runs/<id>/artifacts/
  fs.mkdirSync(path.join(state, "grok-skills", "runs", rid, "artifacts"), {
    recursive: true,
  });

  // Init target git repo with a tracked file.
  const git = (args) =>
    spawnSync("git", ["-C", target, ...args], { encoding: "utf8" });
  git(["init", "-q"]);
  git(["config", "user.name", "t"]);
  git(["config", "user.email", "t@example.com"]);
  git(["config", "commit.gpgsign", "false"]);
  fs.writeFileSync(path.join(target, "hello.txt"), "old\n", "utf8");
  git(["add", "hello.txt"]);
  git(["commit", "-q", "-m", "base"]);

  // Build a real git binary patch from a second worktree-style change.
  const work = path.join(tmp, "work");
  spawnSync("git", ["clone", "-q", target, work], { encoding: "utf8" });
  fs.writeFileSync(path.join(work, "hello.txt"), "new\n", "utf8");
  const diff = spawnSync(
    "git",
    ["-C", work, "diff", "--binary", "HEAD", "--", "hello.txt"],
    { encoding: "utf8" }
  );
  assert.equal(diff.status, 0, diff.stderr);
  assert.ok(diff.stdout.includes("hello.txt"));
  const patchPath = path.join(
    state,
    "grok-skills",
    "runs",
    rid,
    "artifacts",
    "implementation.patch"
  );
  fs.writeFileSync(patchPath, diff.stdout, "utf8");

  const envelope = {
    schemaVersion: 1,
    mode: "peer-stop",
    status: "success",
    runId: rid,
    repository: target,
    targetWorkspace: ".",
    response: {
      peer: { integrationReady: true, preview: false },
      integration: { ready: true, blockers: [] },
    },
  };
  const { env, cleanup } = makeFakeWrapper({
    "peer-stop": {
      stdout: `${JSON.stringify(envelope)}\n`,
      exitCode: 0,
    },
  });
  try {
    const res = runCompanion(["peer-stop", "--run-id", rid, "--integration", "auto"], {
      env: {
        ...env,
        XDG_STATE_HOME: state,
        GROK_DISABLE_ACP: "",
      },
      cwd: target,
    });
    assert.equal(res.code, 0, res.stderr);
    assert.match(res.stderr || "", /applied/i);
    const body = fs.readFileSync(path.join(target, "hello.txt"), "utf8");
    assert.equal(body, "new\n", "auto must apply the verified patch");
  } finally {
    cleanup();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("peer-stop ready + integration=review does not apply", () => {
  const rid = "20260717T120000Z-abc102";
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-review-"));
  const target = path.join(tmp, "target");
  const state = path.join(tmp, "state");
  fs.mkdirSync(target, { recursive: true });
  fs.mkdirSync(path.join(state, "grok-skills", "runs", rid, "artifacts"), {
    recursive: true,
  });

  const git = (args) =>
    spawnSync("git", ["-C", target, ...args], { encoding: "utf8" });
  git(["init", "-q"]);
  git(["config", "user.name", "t"]);
  git(["config", "user.email", "t@example.com"]);
  git(["config", "commit.gpgsign", "false"]);
  fs.writeFileSync(path.join(target, "hello.txt"), "old\n", "utf8");
  git(["add", "hello.txt"]);
  git(["commit", "-q", "-m", "base"]);

  const work = path.join(tmp, "work");
  spawnSync("git", ["clone", "-q", target, work], { encoding: "utf8" });
  fs.writeFileSync(path.join(work, "hello.txt"), "new\n", "utf8");
  const diff = spawnSync(
    "git",
    ["-C", work, "diff", "--binary", "HEAD", "--", "hello.txt"],
    { encoding: "utf8" }
  );
  fs.writeFileSync(
    path.join(state, "grok-skills", "runs", rid, "artifacts", "implementation.patch"),
    diff.stdout,
    "utf8"
  );

  const envelope = {
    schemaVersion: 1,
    mode: "peer-stop",
    status: "success",
    runId: rid,
    repository: target,
    targetWorkspace: ".",
    response: {
      peer: { integrationReady: true, preview: false },
      integration: { ready: true, blockers: [] },
    },
  };
  const { env, cleanup } = makeFakeWrapper({
    "peer-stop": {
      stdout: `${JSON.stringify(envelope)}\n`,
      exitCode: 0,
    },
  });
  try {
    const res = runCompanion(
      ["peer-stop", "--run-id", rid, "--integration", "review"],
      {
        env: {
          ...env,
          XDG_STATE_HOME: state,
          GROK_DISABLE_ACP: "",
        },
        cwd: target,
      }
    );
    assert.equal(res.code, 0, res.stderr);
    assert.match(res.stderr || "", /not applied|leave patch|review/i);
    const body = fs.readFileSync(path.join(target, "hello.txt"), "utf8");
    assert.equal(body, "old\n", "review must not apply the patch");
  } finally {
    cleanup();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
