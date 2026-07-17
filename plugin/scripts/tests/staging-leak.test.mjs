// plugin/scripts/tests/staging-leak.test.mjs
//
// Regression: --task-file - stages a 0600 temp under TMPDIR; every exit path
// after successful staging must remove it (finding 2 from adversarial review).

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

function withPrivateTmp() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-leaktest-"));
  return { tmp, cleanup: () => fs.rmSync(tmp, { recursive: true, force: true }) };
}

function stagedLeftovers(tmp) {
  return fs.readdirSync(tmp).filter((d) => d.startsWith("grok-task-"));
}

test("jobs after --task-file - staging leaves no staged temp behind", () => {
  const { tmp, cleanup } = withPrivateTmp();
  const fake = makeFakeWrapper({});
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-leakcwd-"));
  try {
    const res = runCompanion(["jobs", "--task-file", "-"], {
      env: { ...fake.env, TMPDIR: tmp },
      cwd,
      stdin: "sensitive task text",
    });
    assert.equal(res.code, 0);
    assert.deepEqual(stagedLeftovers(tmp), []);
  } finally {
    fake.cleanup();
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("debate --task-file - cleans the original staged file", () => {
  const { tmp, cleanup } = withPrivateTmp();
  const envelope = JSON.stringify({
    status: "success",
    runId: "20260717T000000Z-abc123",
    mode: "reason",
  });
  const fake = makeFakeWrapper({ reason: { stdout: envelope, exitCode: 0 } });
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-leakcwd-"));
  try {
    const res = runCompanion(["debate", "--task-file", "-"], {
      env: { ...fake.env, TMPDIR: tmp },
      cwd,
      stdin: "debate topic text",
    });
    assert.equal(res.code, 0);
    assert.deepEqual(stagedLeftovers(tmp), []);
  } finally {
    fake.cleanup();
    cleanup();
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
