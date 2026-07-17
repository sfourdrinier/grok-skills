// plugin/scripts/tests/fake-wrapper.test.mjs
//
// Contract tests for the canonical fake-wrapper harness (tests/helpers/
// fake-wrapper.mjs): companion tests never spawn the real wrapper or the Grok
// CLI; they register per-mode canned responses instead.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  companionIsolation,
  makeFakeWrapper,
  readCalls,
  runCompanion,
} from "./helpers/fake-wrapper.mjs";

const RID = "20260716T000000Z-abc123";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-fakewrap-"));
}

test("companionIsolation injects temp XDG, TMPDIR, CLAUDE_PLUGIN_DATA, and cwd", () => {
  const iso = companionIsolation({});
  try {
    assert.ok(path.isAbsolute(iso.cwd));
    assert.ok(path.isAbsolute(iso.env.XDG_STATE_HOME));
    assert.ok(path.isAbsolute(iso.env.TMPDIR));
    assert.ok(path.isAbsolute(iso.env.CLAUDE_PLUGIN_DATA));
    assert.ok(iso.env.XDG_STATE_HOME.startsWith(os.tmpdir()));
    assert.ok(iso.env.TMPDIR.startsWith(os.tmpdir()));
    assert.equal(iso.env.TMP, iso.env.TMPDIR);
    assert.equal(iso.env.TEMP, iso.env.TMPDIR);
    assert.ok(iso.env.CLAUDE_PLUGIN_DATA.startsWith(iso.cwd));
    // Must not point at the real user XDG state root.
    const realXdg = path.join(os.homedir(), ".local", "state");
    assert.notEqual(iso.env.XDG_STATE_HOME, realXdg);
    assert.ok(!iso.env.XDG_STATE_HOME.startsWith(realXdg + path.sep));
  } finally {
    iso.cleanup();
  }
});

test("companionIsolation keeps caller-provided cwd and env keys", () => {
  const cwd = tempCwd();
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-iso-xdg-"));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-iso-tmp-"));
  const pdata = path.join(cwd, "pdata");
  try {
    const iso = companionIsolation({
      cwd,
      env: {
        XDG_STATE_HOME: xdg,
        TMPDIR: tmp,
        CLAUDE_PLUGIN_DATA: pdata,
      },
    });
    try {
      assert.equal(iso.cwd, cwd);
      assert.equal(iso.env.XDG_STATE_HOME, xdg);
      assert.equal(iso.env.TMPDIR, tmp);
      assert.equal(iso.env.CLAUDE_PLUGIN_DATA, pdata);
    } finally {
      // Caller-owned roots must survive iso.cleanup.
      iso.cleanup();
    }
    assert.ok(fs.existsSync(cwd));
    assert.ok(fs.existsSync(xdg));
    assert.ok(fs.existsSync(tmp));
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
    fs.rmSync(xdg, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("runCompanion defaults isolate from process.cwd and real XDG", () => {
  const envelope = JSON.stringify({ status: "success", runId: RID, mode: "status" });
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: envelope, exitCode: 0 },
  });
  try {
    // No cwd / XDG: harness must still succeed without touching the real workspace.
    const res = runCompanion(["status", "--run-id", RID], { env });
    assert.equal(res.code, 0, `stderr: ${res.stderr}`);
    assert.ok(res.stdout.includes(envelope));
  } finally {
    cleanup();
  }
});

test("fake wrapper answers per-mode and companion relays it", () => {
  const envelope = JSON.stringify({ status: "success", runId: RID, mode: "status" });
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: envelope, exitCode: 0 },
  });
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.equal(res.code, 0);
    assert.ok(res.stdout.includes(envelope), `stdout missing envelope: ${res.stdout}`);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("fake wrapper nonzero exit propagates", () => {
  const { env, cleanup } = makeFakeWrapper({ status: { stdout: "{}", exitCode: 1 } });
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.equal(res.code, 1);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("unregistered mode exits 2 (the handoff-not-spawned probe)", () => {
  const { env, cleanup } = makeFakeWrapper({});
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.equal(res.code, 2);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("fake wrapper stderr is relayed", () => {
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: "{}", exitCode: 0, stderr: "[fake] diagnostic line\n" },
  });
  const cwd = tempCwd();
  try {
    const res = runCompanion(["status", "--run-id", RID], { env, cwd });
    assert.match(res.stderr, /\[fake\] diagnostic line/);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("FAKE_WRAPPER_CALLS appends invoked mode; readCalls returns string[]", () => {
  const cwd = tempCwd();
  const callsPath = path.join(cwd, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    status: { stdout: "{}", exitCode: 0 },
  });
  try {
    assert.deepEqual(readCalls(callsPath), []);
    const res = runCompanion(["status", "--run-id", RID], {
      env: { ...env, FAKE_WRAPPER_CALLS: callsPath },
      cwd,
    });
    assert.equal(res.code, 0);
    assert.deepEqual(readCalls(callsPath), ["status"]);
    // Second call appends
    runCompanion(["status", "--run-id", RID], {
      env: { ...env, FAKE_WRAPPER_CALLS: callsPath },
      cwd,
    });
    assert.deepEqual(readCalls(callsPath), ["status", "status"]);
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("echoTask reads --task-file and returns taskEcho + argv", () => {
  const cwd = tempCwd();
  const taskFile = path.join(cwd, "task.txt");
  fs.writeFileSync(taskFile, "literal $(nope)\n", "utf8");
  const { env, cleanup } = makeFakeWrapper({
    verify: { echoTask: true },
  });
  try {
    const res = runCompanion(
      ["verify", "--worktree", "/x", "--task-file", taskFile],
      { env, cwd }
    );
    assert.equal(res.code, 0, res.stderr);
    const envelope = JSON.parse(res.stdout.trim());
    assert.equal(envelope.taskEcho, "literal $(nope)\n");
    assert.ok(envelope.argv.includes(taskFile));
  } finally {
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
