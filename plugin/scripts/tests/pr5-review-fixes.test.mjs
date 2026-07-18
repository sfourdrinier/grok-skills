// plugin/scripts/tests/pr5-review-fixes.test.mjs
//
// Unit coverage for PR #5 Codex-review remediation (batch C companion fixes).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { parseTargetFlag } from "../lib/git-context.mjs";
import { parseDirtyStatusPaths, parseNumstatPaths } from "../lib/integrate.mjs";
import { gateIntegrationForCodeish } from "../lib/jobs.mjs";
import { writeHandoffConsumedMarker } from "../subagent-stop-hook.mjs";

test("parseTargetFlag: last --target wins (matches wrapper argparse)", () => {
  assert.equal(parseTargetFlag(["--target", "a", "--target", "b"]), "b");
  assert.equal(parseTargetFlag(["--target=a", "--target=b"]), "b");
  assert.equal(parseTargetFlag(["--task", "x"]), ".");
  assert.equal(parseTargetFlag(["--target", "only"]), "only");
});

test("gateIntegrationForCodeish: continue-run is exempt from direct consent", () => {
  // Fresh workspace (no consent recorded) + default direct would normally refuse;
  // continue-run must pass through untouched.
  const res = gateIntegrationForCodeish(
    "code",
    ["--continue-run", "20260101T000000Z-abc123", "--task-file", "-"],
    null,
    os.tmpdir(),
    {}
  );
  assert.equal(res.ok, true);
  assert.equal(res.effective, null);
});

test("[3] gateIntegrationForCodeish routes implement to worktree (never direct)", () => {
  const res = gateIntegrationForCodeish(
    "implement",
    ["--target", ".", "--base", "HEAD", "--task-file", "-"],
    null,
    os.tmpdir(),
    {}
  );
  assert.equal(res.ok, true);
  assert.equal(res.effective, "worktree");
  const i = res.rest.indexOf("--integration");
  assert.ok(i >= 0 && res.rest[i + 1] === "worktree", res.rest.join(" "));
});

test("[11] gateIntegrationForCodeish exempts --continue-run= (equals form)", () => {
  const res = gateIntegrationForCodeish(
    "code",
    ["--continue-run=20260101T000000Z-abc123", "--task-file", "-"],
    null,
    os.tmpdir(),
    {}
  );
  assert.equal(res.ok, true);
  assert.equal(res.effective, null);
});

test("parseDirtyStatusPaths: extracts modified/untracked/renamed paths", () => {
  const status = " M src/a.js\n?? new.txt\nR  old.js -> renamed.js\n";
  const set = parseDirtyStatusPaths(status);
  assert.ok(set.has("src/a.js"));
  assert.ok(set.has("new.txt"));
  assert.ok(set.has("old.js"));
  assert.ok(set.has("renamed.js"));
});

test("parseNumstatPaths: extracts patch target paths incl. BOTH rename sides + raw", () => {
  const numstat = "1\t0\tsrc/a.js\n-\t-\tbin.dat\n2\t1\t{old => new}/f.js\n0\t0\ta.js => b.js\n";
  const paths = parseNumstatPaths(numstat);
  // Ordinary paths appear once; rename-looking fields yield both sides AND the
  // raw field (so a filename literally containing " => " is still checked).
  assert.deepEqual(paths, [
    "src/a.js",
    "bin.dat",
    "old/f.js",
    "new/f.js",
    "{old => new}/f.js",
    "a.js",
    "b.js",
    "a.js => b.js",
  ]);
});

test("parseNumstatPaths: a real filename containing ' => ' keeps its raw path", () => {
  // git does NOT quote ` => ` in a path; the rename-split mis-parses it, but the
  // raw field is retained so the dirty-overlap guard can still match it.
  const paths = parseNumstatPaths("1\t0\tweird => name.txt\n");
  assert.ok(paths.includes("weird => name.txt"), paths.join(","));
});

test("writeHandoffConsumedMarker: writes marker under the run dir", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-marker-"));
  try {
    const runId = "20260101T000000Z-abc123";
    const runDir = path.join(root, "grok-skills", "runs", runId);
    fs.mkdirSync(runDir, { recursive: true });
    const ok = writeHandoffConsumedMarker(runId, { XDG_STATE_HOME: root });
    assert.equal(ok, true);
    assert.ok(fs.existsSync(path.join(runDir, "handoff-consumed.json")));
    // Missing run dir -> false, no throw.
    assert.equal(
      writeHandoffConsumedMarker("20260101T000000Z-missing", { XDG_STATE_HOME: root }),
      false
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("importing subagent-stop-hook does not run the hook (no stdin read/exit)", () => {
  // Guard regression: the module must be import-safe for the companion.
  const res = spawnSync(
    process.execPath,
    ["-e", "import('../subagent-stop-hook.mjs').then(m => console.log(typeof m.writeHandoffConsumedMarker))"],
    { cwd: path.dirname(new URL(import.meta.url).pathname), encoding: "utf8", timeout: 10000 }
  );
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stdout, /function/);
});
