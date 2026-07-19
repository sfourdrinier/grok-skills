// plugin/scripts/tests/companion-capture.test.mjs
//
// Capture path contracts: onStdout throw must synthesize one complete failure
// envelope (applied=false, integration-error), nonzero job/store/notify - never
// leave raw wrapper ready success on stdout/store.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { createCaptureAndTrack } from "../lib/companion-capture.mjs";
import { listJobs, readJobStdout } from "../lib/jobs.mjs";

const RUN_ID = "20260718T010000Z-cap001";

function tempCwd() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "grok-capture-"));
}

test("onStdout throw: synthesizes complete failure envelope; nonzero job/store", async () => {
  const cwd = tempCwd();
  const pluginData = path.join(cwd, "pdata");
  const prevPlugin = process.env.CLAUDE_PLUGIN_DATA;
  const prevXdg = process.env.XDG_STATE_HOME;
  process.env.CLAUDE_PLUGIN_DATA = pluginData;
  process.env.XDG_STATE_HOME = path.join(cwd, "xdg");

  // Fake python that prints a ready peer-stop success envelope then exits 0.
  const py = path.join(cwd, "fake-py.py");
  const ready = {
    schemaVersion: 1,
    status: "success",
    mode: "peer-stop",
    runId: RUN_ID,
    repository: cwd,
    response: { peer: { integrationReady: true }, integration: { ready: true } },
  };
  fs.writeFileSync(
    py,
    `import sys\nsys.stdout.write(${JSON.stringify(JSON.stringify(ready) + "\\n")})\nsys.exit(0)\n`
  );

  const stderr = [];
  const writes = [];
  const origWrite = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, enc, cb) => {
    writes.push(String(chunk));
    if (typeof enc === "function") enc();
    else if (typeof cb === "function") cb();
    return true;
  };

  let code;
  try {
    const captureAndTrack = createCaptureAndTrack({
      python: "python3",
      pluginRoot: cwd,
      spawnFailedExit: 4,
      signalExit: 1,
      spawnFailedMessage: (w, d) => `spawn failed ${w}: ${d}\n`,
      stderrLine: (l) => stderr.push(l),
    });
    code = await captureAndTrack(py, ["peer-stop"], {
      cwd,
      mode: "peer-stop",
      kind: "run",
      runMode: "hardened",
      notifyMode: "peer-stop",
      skipNotify: true,
      onStdout: () => {
        throw new Error("forced onStdout boom");
      },
    });
  } finally {
    process.stdout.write = origWrite;
    if (prevPlugin === undefined) delete process.env.CLAUDE_PLUGIN_DATA;
    else process.env.CLAUDE_PLUGIN_DATA = prevPlugin;
    if (prevXdg === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prevXdg;
  }

  assert.notEqual(code, 0, "hook throw must yield nonzero exit");
  assert.equal(code, 1);
  const stdout = writes.join("");
  const line = stdout
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.startsWith("{"))
    .pop();
  assert.ok(line, `expected one JSON envelope; got: ${stdout}`);
  const env = JSON.parse(line);
  assert.equal(env.status, "failure");
  assert.notEqual(env.status, "success");
  assert.equal(env.response?.integration?.applied, false);
  assert.equal(env.response?.integration?.ready, false);
  assert.equal(env.response?.integration?.outcome, "integration-error");
  assert.equal(env.response?.peer?.integrationReady, false, "must clear peer.integrationReady");
  assert.equal(typeof env.error?.class === "string" ? env.error.class : "integration-error", "integration-error");

  // Job store must also hold the failure envelope (not raw ready success).
  process.env.CLAUDE_PLUGIN_DATA = pluginData;
  try {
    const jobs = listJobs(cwd, { CLAUDE_PLUGIN_DATA: pluginData });
    assert.ok(jobs.length >= 1);
    const job = jobs[0];
    assert.equal(job.status, "failure");
    const stored = readJobStdout(cwd, job.id, { CLAUDE_PLUGIN_DATA: pluginData });
    assert.ok(stored);
    const storedEnv = JSON.parse(String(stored).trim().split("\n").filter(Boolean).pop());
    assert.equal(storedEnv.status, "failure");
    assert.equal(storedEnv.response?.integration?.applied, false);
    assert.equal(storedEnv.response?.integration?.outcome, "integration-error");
  } finally {
    if (prevPlugin === undefined) delete process.env.CLAUDE_PLUGIN_DATA;
    else process.env.CLAUDE_PLUGIN_DATA = prevPlugin;
    fs.rmSync(cwd, { recursive: true, force: true });
  }

  assert.ok(
    stderr.some((l) => /onStdout|hook failed|integration-error|boom/i.test(l)),
    stderr.join("\n")
  );
});
