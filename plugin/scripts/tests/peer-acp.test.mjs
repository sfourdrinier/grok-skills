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
import {
  listJobs,
  readJobStdout,
} from "../lib/jobs.mjs";
import { createHash } from "node:crypto";

/** Write the validation manifest the companion apply re-verifies (sha/bytes). */
function stageHandoffManifest(stateDir, runId, patchBody) {
  const buf = Buffer.from(patchBody);
  fs.writeFileSync(
    path.join(stateDir, "grok-skills", "runs", runId, "implementation-handoff.json"),
    JSON.stringify({
      patch: {
        sha256: createHash("sha256").update(buf).digest("hex"),
        bytes: buf.length,
        relativePath: "artifacts/implementation.patch",
      },
    })
  );
}

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

test("peer-start background: running resident detaches stdout so library await does not hang", async () => {
  // Confirmed medium finding: after the first status=running envelope,
  // runPeerStartBackground only child.unref()s and leaves the stdout pipe
  // listener/socket attached. Library callers without an outer process.exit
  // then hang even though the helper already resolved. The resident must stay
  // alive; only the companion-side capture handles detach.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "peer-start-detach-"));
  const pidPath = path.join(dir, "resident.pid");
  const resultPath = path.join(dir, "harness-result.json");
  const fake = path.join(dir, "resident.mjs");
  const harness = path.join(dir, "harness.mjs");
  const peerAcpUrl = path.resolve(SCRIPT_DIR, "../lib/peer-acp.mjs");
  fs.writeFileSync(
    fake,
    `
import fs from "node:fs";
fs.writeFileSync(${JSON.stringify(pidPath)}, String(process.pid));
process.stdout.write(${JSON.stringify(
      JSON.stringify({ status: "running", mode: "peer-start", runId: "detach-x" }) + "\n"
    )});
// Stay resident (control-socket stand-in). Do not exit.
setInterval(() => {}, 1 << 30);
`
  );
  fs.writeFileSync(
    harness,
    `
import fs from "node:fs";
import { runPeerStartBackground } from ${JSON.stringify(peerAcpUrl)};
const code = await runPeerStartBackground(process.execPath, ${JSON.stringify(fake)}, [], {
  spawnFailedMessage: () => "",
  signalExit: 1,
  spawnFailedExit: 4,
});
fs.writeFileSync(${JSON.stringify(resultPath)}, JSON.stringify({ code }));
// Natural event-loop drain - no process.exit. Attached stdout/listeners on a
// live resident must not keep this harness alive.
`
  );
  let residentPid = null;
  try {
    const harnessProc = spawnSync(process.execPath, [harness], {
      encoding: "utf8",
      timeout: 4000,
      env: process.env,
    });
    assert.notEqual(
      harnessProc.error?.code,
      "ETIMEDOUT",
      "library await must not hang after the running envelope (stdout still attached?)"
    );
    assert.equal(
      harnessProc.status,
      0,
      `harness must exit 0 after resolve; status=${harnessProc.status} stderr=${harnessProc.stderr}`
    );
    assert.ok(fs.existsSync(resultPath), "harness must record the resolved exit code");
    const { code } = JSON.parse(fs.readFileSync(resultPath, "utf8"));
    assert.equal(code, 0, "running envelope must resolve 0");
    assert.ok(fs.existsSync(pidPath), "resident must have written its pid");
    residentPid = Number(fs.readFileSync(pidPath, "utf8"));
    assert.ok(Number.isInteger(residentPid) && residentPid > 0, "resident pid");
    // Detach must not kill the resident - only drop companion capture handles.
    assert.doesNotThrow(
      () => process.kill(residentPid, 0),
      "resident must still be alive after successful running-path detach"
    );
  } finally {
    if (residentPid) {
      try {
        process.kill(residentPid, "SIGTERM");
      } catch {
        /* already gone */
      }
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("peer-start background: durable stderr log is created private (0600)", async () => {
  // The resident peer later writes repo paths + operational diagnostics to this
  // long-lived /tmp file; another local user must not be able to read it.
  const { file, cleanup } = fakeEnvelopeWrapper(
    JSON.stringify({ status: "running", mode: "peer-start", runId: "x" })
  );
  const prefix = `grok-peer-start-${process.pid}-`;
  const listLogs = () =>
    fs.readdirSync(os.tmpdir()).filter((f) => f.startsWith(prefix) && f.endsWith(".log"));
  const before = new Set(listLogs());
  try {
    await runPeerStartBackground(process.execPath, file, [], {
      spawnFailedMessage: () => "",
      signalExit: 1,
      spawnFailedExit: 4,
    });
    const created = listLogs().filter((f) => !before.has(f));
    assert.ok(created.length >= 1, "a durable stderr log must be created");
    for (const f of created) {
      const full = path.join(os.tmpdir(), f);
      const mode = fs.statSync(full).mode & 0o777;
      assert.equal(mode, 0o600, `peer stderr log ${f} must be 0600, got ${mode.toString(8)}`);
      fs.rmSync(full, { force: true });
    }
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
  stageHandoffManifest(state, rid, diff.stdout);

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

test("peer-stop job is marked FAILED when the apply is blocked (job-status honesty)", () => {
  // A ready peer-stop whose apply is refused (direct integration, no consent)
  // must NOT leave /grok:jobs showing the job as successful.
  const rid = "20260717T120000Z-abc199";
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-jobfail-"));
  const target = path.join(tmp, "target");
  const state = path.join(tmp, "state");
  const pluginData = path.join(tmp, "pdata");
  fs.mkdirSync(target, { recursive: true });
  fs.mkdirSync(path.join(state, "grok-skills", "runs", rid, "artifacts"), { recursive: true });
  const git = (args) => spawnSync("git", ["-C", target, ...args], { encoding: "utf8" });
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
  const diff = spawnSync("git", ["-C", work, "diff", "--binary", "HEAD", "--", "hello.txt"], {
    encoding: "utf8",
  });
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
    response: { peer: { integrationReady: true }, integration: { ready: true, blockers: [] } },
  };
  const { env, cleanup } = makeFakeWrapper({
    "peer-stop": { stdout: `${JSON.stringify(envelope)}\n`, exitCode: 0 },
  });
  try {
    const runEnv = {
      ...env,
      XDG_STATE_HOME: state,
      CLAUDE_PLUGIN_DATA: pluginData,
      GROK_DISABLE_ACP: "",
    };
    // direct integration with NO recorded consent -> apply blocked (consent-required).
    const res = runCompanion(["peer-stop", "--run-id", rid, "--integration", "direct"], {
      env: runEnv,
      cwd: target,
    });
    assert.equal(res.code, 1, `blocked apply must exit nonzero; stderr: ${res.stderr}`);
    assert.equal(
      fs.readFileSync(path.join(target, "hello.txt"), "utf8"),
      "old\n",
      "target must be untouched (no apply)"
    );
    const jobs = listJobs(target, { CLAUDE_PLUGIN_DATA: pluginData, XDG_STATE_HOME: state });
    assert.ok(jobs.length >= 1, "a peer-stop job was recorded");
    assert.equal(jobs[0].status, "failure", "the peer-stop job must be marked failed");
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

/**
 * Fixture for peer-stop completion-path honesty: ready wrapper envelope + real
 * patch + optional dirty target. Returns everything needed to assert the full
 * chain (stdout / stored / job / notify / target bytes).
 */
function stagePeerStopCompletionFixture({
  rid,
  integration,
  dirtyTarget = false,
}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-complete-"));
  const target = path.join(tmp, "target");
  const state = path.join(tmp, "state");
  const pluginData = path.join(tmp, "pdata");
  fs.mkdirSync(target, { recursive: true });
  fs.mkdirSync(path.join(state, "grok-skills", "runs", rid, "artifacts"), {
    recursive: true,
  });
  const git = (args) => spawnSync("git", ["-C", target, ...args], { encoding: "utf8" });
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
  const diff = spawnSync("git", ["-C", work, "diff", "--binary", "HEAD", "--", "hello.txt"], {
    encoding: "utf8",
  });
  assert.equal(diff.status, 0, diff.stderr);
  fs.writeFileSync(
    path.join(state, "grok-skills", "runs", rid, "artifacts", "implementation.patch"),
    diff.stdout,
    "utf8"
  );
  stageHandoffManifest(state, rid, diff.stdout);
  if (dirtyTarget) {
    // Overlap the patch path so the dirty-overlap guard blocks apply.
    fs.writeFileSync(path.join(target, "hello.txt"), "diverged\n", "utf8");
  }
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
  const { env, cleanup: cleanupWrapper } = makeFakeWrapper({
    "peer-stop": { stdout: `${JSON.stringify(envelope)}\n`, exitCode: 0 },
  });
  // Notifications stay off (default): peer-stop is not notify-eligible, and we
  // avoid external webhook attempts. Lifecycle honesty is still asserted if a
  // marker is ever written.
  const runEnv = {
    ...env,
    XDG_STATE_HOME: state,
    CLAUDE_PLUGIN_DATA: pluginData,
    GROK_DISABLE_ACP: "",
    GROK_COMPANION_EXECUTION_CONTEXT: "background",
  };
  return {
    tmp,
    target,
    state,
    pluginData,
    runEnv,
    integration,
    rid,
    dirtyTarget,
    cleanup: () => {
      cleanupWrapper();
      fs.rmSync(tmp, { recursive: true, force: true });
    },
  };
}

function assertPeerStopBlockedCompletion(res, fx, expectedOutcome) {
  assert.notEqual(res.code, 0, `blocked apply must exit nonzero; stderr: ${res.stderr}`);
  const envLines = (res.stdout || "").split("\n").filter((l) => l.trim().startsWith("{"));
  assert.equal(envLines.length, 1, `exactly one stdout envelope; got: ${res.stdout}`);
  const finalEnv = JSON.parse(envLines[0]);
  assert.equal(finalEnv.status, "failure", `stdout must not report success; got: ${res.stdout}`);
  assert.notEqual(finalEnv.status, "success");
  assert.equal(finalEnv.response?.integration?.applied, false);
  assert.equal(finalEnv.response?.integration?.outcome, expectedOutcome);
  const jobs = listJobs(fx.target, {
    CLAUDE_PLUGIN_DATA: fx.pluginData,
    XDG_STATE_HOME: fx.state,
  });
  assert.ok(jobs.length >= 1, "a peer-stop job was recorded");
  assert.equal(jobs[0].status, "failure", "job must be failed for a blocked apply");
  const stored = readJobStdout(fx.target, jobs[0].id, {
    CLAUDE_PLUGIN_DATA: fx.pluginData,
    XDG_STATE_HOME: fx.state,
  });
  assert.ok(stored, "stored result must exist");
  assert.equal(stored.trim(), envLines[0].trim(), "stored result must match the final stdout envelope");
  const storedEnv = JSON.parse(stored);
  assert.equal(storedEnv.status, "failure");
  assert.equal(storedEnv.response?.integration?.applied, false);
  assert.equal(storedEnv.response?.integration?.outcome, expectedOutcome);
  const expectedBody = fx.dirtyTarget ? "diverged\n" : "old\n";
  assert.equal(
    fs.readFileSync(path.join(fx.target, "hello.txt"), "utf8"),
    expectedBody,
    "target must be untouched when apply is blocked"
  );
  const notifiedPath = path.join(
    fx.state,
    "grok-skills",
    "runs",
    fx.rid,
    "notified.json"
  );
  if (fs.existsSync(notifiedPath)) {
    const marker = JSON.parse(fs.readFileSync(notifiedPath, "utf8"));
    assert.notEqual(
      marker.lifecycle,
      "completed",
      "blocked peer-stop must not notify lifecycle=completed"
    );
    assert.equal(marker.lifecycle, "failed");
  }
}

test("peer-stop consent-blocked: final stdout/stored envelope is failure with applied=false", () => {
  // Wrapper returns ready+success, but direct integration has no consent. The
  // companion completion path must rewrite the FINAL envelope (and stored
  // /grok:result payload) before first stdout write so consumers never see a
  // success envelope for an unapplied peer-stop.
  const rid = "20260717T120000Z-abc301";
  const fx = stagePeerStopCompletionFixture({
    rid,
    integration: "direct",
  });
  try {
    const res = runCompanion(["peer-stop", "--run-id", rid, "--integration", "direct"], {
      env: fx.runEnv,
      cwd: fx.target,
    });
    assertPeerStopBlockedCompletion(res, fx, "consent-required");
  } finally {
    fx.cleanup();
  }
});

test("peer-stop dirty-overlap blocked: final stdout/stored envelope is failure with applied=false", () => {
  const rid = "20260717T120000Z-abc302";
  const fx = stagePeerStopCompletionFixture({
    rid,
    integration: "auto",
    dirtyTarget: true,
  });
  try {
    const res = runCompanion(["peer-stop", "--run-id", rid, "--integration", "auto"], {
      env: fx.runEnv,
      cwd: fx.target,
    });
    assertPeerStopBlockedCompletion(res, fx, "blocked-dirty-overlap");
  } finally {
    fx.cleanup();
  }
});

test("peer-stop success apply: still exactly one success envelope with applied=true", () => {
  const rid = "20260717T120000Z-abc303";
  const fx = stagePeerStopCompletionFixture({ rid, integration: "auto" });
  try {
    const res = runCompanion(["peer-stop", "--run-id", rid, "--integration", "auto"], {
      env: fx.runEnv,
      cwd: fx.target,
    });
    assert.equal(res.code, 0, res.stderr);
    const envLines = (res.stdout || "").split("\n").filter((l) => l.trim().startsWith("{"));
    assert.equal(envLines.length, 1, `exactly one stdout envelope; got: ${res.stdout}`);
    const finalEnv = JSON.parse(envLines[0]);
    assert.equal(finalEnv.status, "success");
    assert.equal(finalEnv.response?.integration?.applied, true);
    assert.equal(finalEnv.response?.integration?.outcome, "applied");
    assert.equal(fs.readFileSync(path.join(fx.target, "hello.txt"), "utf8"), "new\n");
    const jobs = listJobs(fx.target, {
      CLAUDE_PLUGIN_DATA: fx.pluginData,
      XDG_STATE_HOME: fx.state,
    });
    assert.ok(jobs.length >= 1);
    assert.equal(jobs[0].status, "success");
    const stored = readJobStdout(fx.target, jobs[0].id, {
      CLAUDE_PLUGIN_DATA: fx.pluginData,
      XDG_STATE_HOME: fx.state,
    });
    assert.equal(stored.trim(), envLines[0].trim());
  } finally {
    fx.cleanup();
  }
});
