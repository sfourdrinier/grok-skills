// plugin/scripts/tests/pr5-review-fixes.test.mjs
//
// Unit coverage for PR #5 Codex-review remediation (batch C companion fixes).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { parseTargetFlag } from "../lib/git-context.mjs";
import { parseDirtyStatusPaths, parseNumstatPaths } from "../lib/integrate.mjs";
import { gateIntegrationForCodeish, resolveContinueRunTargetWorkspace } from "../lib/jobs.mjs";
import { runsDirFor } from "../progress-relay.mjs";
import { writeHandoffConsumedMarker } from "../subagent-stop-hook.mjs";
import { makeFakeWrapper, runCompanion } from "./helpers/fake-wrapper.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

test("parseTargetFlag: last --target wins (matches wrapper argparse)", () => {
  assert.equal(parseTargetFlag(["--target", "a", "--target", "b"]), "b");
  assert.equal(parseTargetFlag(["--target=a", "--target=b"]), "b");
  assert.equal(parseTargetFlag(["--task", "x"]), ".");
  assert.equal(parseTargetFlag(["--target", "only"]), "only");
});

test("gateIntegrationForCodeish: continue-run is exempt from direct consent", () => {
  // Fresh workspace (no consent recorded) + default direct would normally refuse;
  // continue-run stays consent-exempt but still resolves effective integration.
  const res = gateIntegrationForCodeish(
    "code",
    ["--continue-run", "20260101T000000Z-abc123", "--task-file", "-"],
    null,
    os.tmpdir(),
    {}
  );
  assert.equal(res.ok, true);
  assert.equal(res.continueRun, true);
  // Default integration is direct: companion maps wrapper lineage to worktree
  // without auto apply, while preserving effective=direct for mode awareness.
  assert.equal(res.effective, "direct");
  const i = res.rest.indexOf("--integration");
  assert.ok(i >= 0 && res.rest[i + 1] === "worktree", res.rest.join(" "));
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
  assert.equal(res.continueRun, true);
  assert.equal(res.effective, "direct");
  const i = res.rest.indexOf("--integration");
  assert.ok(i >= 0 && res.rest[i + 1] === "worktree", res.rest.join(" "));
});

test("gateIntegrationForCodeish: continue-run auto keeps effective=auto (apply-on-ready)", () => {
  const res = gateIntegrationForCodeish(
    "code",
    ["--continue-run", "20260101T000000Z-abc123", "--task-file", "-"],
    "auto",
    os.tmpdir(),
    {}
  );
  assert.equal(res.ok, true);
  assert.equal(res.continueRun, true);
  assert.equal(res.effective, "auto");
  // Wrapper still receives worktree lineage; companion auto path uses effective.
  const i = res.rest.indexOf("--integration");
  assert.ok(i >= 0 && res.rest[i + 1] === "worktree", res.rest.join(" "));
});

test("gateIntegrationForCodeish: continue-run review retains (effective=review)", () => {
  const res = gateIntegrationForCodeish(
    "code",
    ["--continue-run=20260101T000000Z-abc123"],
    "review",
    os.tmpdir(),
    {}
  );
  assert.equal(res.ok, true);
  assert.equal(res.continueRun, true);
  assert.equal(res.effective, "review");
  const i = res.rest.indexOf("--integration");
  assert.ok(i >= 0 && res.rest[i + 1] === "worktree", res.rest.join(" "));
});

test("parseDirtyStatusPaths: extracts modified/untracked/renamed paths (-z)", () => {
  // `git status --porcelain -z`: NUL-terminated; a rename is two NUL tokens.
  const status = " M src/a.js\0?? new.txt\0R  renamed.js\0old.js\0";
  const set = parseDirtyStatusPaths(status);
  assert.ok(set.has("src/a.js"));
  assert.ok(set.has("new.txt"));
  assert.ok(set.has("old.js"));
  assert.ok(set.has("renamed.js"));
});

test("parseDirtyStatusPaths: -z paths with a literal ' -> ' are never mis-split", () => {
  // Non-rename paths whose literal name contains ' -> ' must enter the set whole,
  // or the real path never registers and the overlap guard fails open.
  const set = parseDirtyStatusPaths(" M weird -> name.js\0?? other -> file.txt\0");
  assert.ok(set.has("weird -> name.js"), [...set].join("|"));
  assert.ok(set.has("other -> file.txt"), [...set].join("|"));
  assert.ok(!set.has("weird"));
  assert.ok(!set.has("name.js"));
  // A rename (R) contributes BOTH the new and paired source path.
  const rn = parseDirtyStatusPaths("R  b.js\0a.js\0");
  assert.ok(rn.has("a.js") && rn.has("b.js"));
  // Copy (C) too.
  const cp = parseDirtyStatusPaths("C  c.js\0a.js\0");
  assert.ok(cp.has("a.js") && cp.has("c.js"));
  // A rename whose SOURCE name literally contains ' -> ' is kept whole (the -z
  // format is unquoted, so there is no arrow to mis-split on).
  const q = parseDirtyStatusPaths("R  c.txt\0a -> b.txt\0");
  assert.ok(q.has("c.txt") && q.has("a -> b.txt"), [...q].join("|"));
  assert.ok(!q.has("a") && !q.has("b.txt"));
});

test("parseNumstatPaths decodes git C-style octal-quoted UTF-8 paths", () => {
  // `git apply --numstat` quotes non-ASCII: "é.txt" -> "\303\251.txt". The dirty
  // set from `git status --porcelain -z` carries the RAW "é.txt", so the numstat
  // path must decode to the same bytes or the overlap guard fails open on it.
  const numstat = '1\t0\t"\\303\\251.txt"\n';
  assert.deepEqual(parseNumstatPaths(numstat), ["é.txt"]);
  // Named escapes still decode; an unquoted path is returned as-is.
  assert.deepEqual(parseNumstatPaths('0\t0\t"a\\tb.js"\n'), ["a\tb.js"]);
  assert.deepEqual(parseNumstatPaths("1\t0\tplain.js\n"), ["plain.js"]);
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

test("resolveContinueRunTargetWorkspace reads prior run.json repository", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-tgt-"));
  const stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-xdg-"));
  const env = { ...process.env, XDG_STATE_HOME: stateHome, CLAUDE_PLUGIN_DATA: path.join(cwd, "pdata") };
  const runId = "20260717T120000Z-abcdef";
  const runsDir = runsDirFor(env);
  const runDir = path.join(runsDir, runId);
  fs.mkdirSync(runDir, { recursive: true });
  const repo = path.join(cwd, "other-repo");
  fs.mkdirSync(repo, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "run.json"),
    JSON.stringify({ repository: repo, targetWorkspace: repo }) + "\n"
  );
  const resolved = resolveContinueRunTargetWorkspace(runId, cwd, env);
  assert.equal(path.resolve(resolved), path.resolve(repo));
  // Missing prior run falls back to cwd workspace root.
  const missing = resolveContinueRunTargetWorkspace("20260717T120000Z-000000", cwd, env);
  assert.equal(path.resolve(missing), path.resolve(cwd));
  fs.rmSync(cwd, { recursive: true, force: true });
  fs.rmSync(stateHome, { recursive: true, force: true });
});

test("resolveContinueRunTargetWorkspace: relative targetWorkspace resolves against rec.repository", () => {
  // Prior run recorded targetWorkspace as package-relative "pkg". Companion is
  // invoked from a foreign cwd; resolution must use rec.repository, not cwd.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-continue-rel-"));
  const foreignCwd = path.join(root, "outside");
  fs.mkdirSync(foreignCwd, { recursive: true });
  // Decoy so a buggy cwd-relative resolve would land here instead of original repo.
  fs.mkdirSync(path.join(foreignCwd, "pkg"), { recursive: true });
  const originalRepo = path.join(root, "original-repo");
  fs.mkdirSync(path.join(originalRepo, "pkg"), { recursive: true });
  // Real git repo so resolveTargetWorkspaceRoot walks to toplevel.
  const git = (args) => {
    const r = spawnSync("git", args, { cwd: originalRepo, encoding: "utf8" });
    assert.equal(r.status, 0, r.stderr);
  };
  git(["init"]);
  git(["config", "user.email", "t@example.com"]);
  git(["config", "user.name", "t"]);
  fs.writeFileSync(path.join(originalRepo, "pkg", "a.txt"), "x\n");
  git(["add", "pkg/a.txt"]);
  git(["commit", "-m", "seed"]);

  const stateHome = path.join(root, "xdg");
  const env = {
    ...process.env,
    XDG_STATE_HOME: stateHome,
    CLAUDE_PLUGIN_DATA: path.join(root, "pdata"),
  };
  const runId = "20260717T120000Z-a1b2c3";
  const runDir = path.join(runsDirFor(env), runId);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "run.json"),
    JSON.stringify({
      runId,
      repository: originalRepo,
      targetWorkspace: "pkg",
    }) + "\n"
  );

  const resolved = resolveContinueRunTargetWorkspace(runId, foreignCwd, env);
  // realpath: macOS /var vs /private/var; git rev-parse may canonicalize.
  assert.equal(
    fs.realpathSync(resolved),
    fs.realpathSync(originalRepo),
    `relative targetWorkspace must key off rec.repository; got ${resolved}`
  );
  assert.notEqual(
    fs.realpathSync(resolved),
    fs.realpathSync(foreignCwd),
    "must not resolve relative targetWorkspace against companion cwd"
  );
  fs.rmSync(root, { recursive: true, force: true });
});

// runHandoff must stamp handoff-consumed via shared last-valid flagValue SSOT
// (split AND equals). indexOf("--run-id") misses --run-id=<id>.
test("runHandoff consumed marker parses --run-id split and equals (shared SSOT)", () => {
  const companionSrc = fs.readFileSync(
    path.join(SCRIPT_DIR, "..", "grok-companion.mjs"),
    "utf8"
  );
  const handoffFn = companionSrc.slice(companionSrc.indexOf("function runHandoff"));
  // Must use shared flagValue (last-valid) - not raw indexOf split-only.
  assert.match(handoffFn, /flagValue\([^)]*--run-id/, "runHandoff must use flagValue(--run-id)");
  assert.doesNotMatch(
    handoffFn,
    /indexOf\(["']--run-id["']\)/,
    "runHandoff must not use indexOf('--run-id') (misses equals form)"
  );

  // Behavioral: prove companion handoff path stamps for --run-id= via fake wrapper.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-handoff-marker-"));
  const xdg = path.join(root, "xdg");
  const runId = "20260717T120000Z-abcdef";
  const runDir = path.join(xdg, "grok-skills", "runs", runId);
  fs.mkdirSync(runDir, { recursive: true });
  const { env, cleanup } = makeFakeWrapper({
    handoff: {
      stdout:
        JSON.stringify({
          schemaVersion: 1,
          status: "success",
          mode: "handoff",
          runId,
          response: { integration: { ready: true, blockers: [] } },
        }) + "\n",
      exitCode: 0,
    },
  });
  try {
    const res = runCompanion(["handoff", `--run-id=${runId}`], {
      cwd: root,
      env: {
        ...env,
        XDG_STATE_HOME: xdg,
        CLAUDE_PLUGIN_DATA: path.join(root, "pdata"),
        GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
      },
    });
    assert.equal(res.code, 0, res.stderr);
    assert.ok(
      fs.existsSync(path.join(runDir, "handoff-consumed.json")),
      "equals-form --run-id= must stamp handoff-consumed.json"
    );
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// companion-setup must reuse hasFlagOrEquals rather than local startsWith loops.
test("companion-setup uses hasFlagOrEquals SSOT (no local startsWith presence loops)", () => {
  const src = fs.readFileSync(path.join(SCRIPT_DIR, "..", "lib", "companion-setup.mjs"), "utf8");
  assert.match(src, /hasFlagOrEquals/, "must import/use hasFlagOrEquals");
  assert.doesNotMatch(
    src,
    /a\.startsWith\("--codex-agents-scope="\)/,
    "no local startsWith for --codex-agents-scope="
  );
  assert.doesNotMatch(
    src,
    /a\.startsWith\("--run-mode="\)/,
    "no local startsWith for --run-mode="
  );
  assert.doesNotMatch(
    src,
    /a\.startsWith\("--integration="\)/,
    "no local startsWith for --integration="
  );
  assert.doesNotMatch(
    src,
    /a\.startsWith\("--notification-mode="\)/,
    "no local startsWith for --notification-mode="
  );
  assert.doesNotMatch(
    src,
    /a\.startsWith\("--notification-webhook-url="\)/,
    "no local startsWith for --notification-webhook-url="
  );
});
