// plugin/scripts/tests/peer-integrate.test.mjs
//
// Task 7.4 remediation: maybeIntegratePeerStop apply path. Real temp git target
// so apply / --check refusal / review-retain are exercised on disk. The
// --check-refusal case proves no half-apply; the reverse-on-failure branch
// mirrors the proven applyVerifiedPatch reverse (auto path is covered in
// integrate-auto.test.mjs).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { maybeIntegratePeerStop } from "../lib/integrate.mjs";

const RUN_ID = "20260717T130000Z-abc123";

function git(cwd, args) {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(r.status, 0, `git ${args.join(" ")} failed: ${r.stderr}`);
  return r;
}

function initRepo(dir) {
  fs.mkdirSync(dir, { recursive: true });
  git(dir, ["init"]);
  git(dir, ["config", "user.email", "t@t.t"]);
  git(dir, ["config", "user.name", "T"]);
  fs.writeFileSync(path.join(dir, "foo.txt"), "hello\n");
  git(dir, ["add", "-A"]);
  git(dir, ["commit", "-m", "init"]);
  return dir;
}

/** Modify foo.txt, capture binary diff vs HEAD, restore original so apply can land. */
function capturePatch(repo) {
  const f = path.join(repo, "foo.txt");
  const original = fs.readFileSync(f, "utf8");
  fs.writeFileSync(f, "hello world\n");
  const d = spawnSync("git", ["diff", "--binary", "HEAD"], { cwd: repo, encoding: "utf8" });
  assert.equal(d.status, 0, d.stderr);
  fs.writeFileSync(f, original);
  return d.stdout;
}

function stagePatch(xdg, runId, body) {
  const art = path.join(xdg, "grok-skills", "runs", runId, "artifacts");
  fs.mkdirSync(art, { recursive: true });
  fs.writeFileSync(path.join(art, "implementation.patch"), body);
}

function peerStopEnvelope(repo, over = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: "success",
    mode: "peer",
    runId: RUN_ID,
    repository: repo,
    targetWorkspace: repo,
    response: { peer: { integrationReady: true } },
    ...over,
  });
}

function withXdg(xdg, fn) {
  const saved = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  try {
    return fn();
  } finally {
    if (saved === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = saved;
  }
}

test("peer-stop auto: applies verified patch to the target tree", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const lines = [];
  try {
    withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
    assert.ok(lines.some((l) => /\bapplied\b/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop auto: --check refusal leaves the tree unchanged (no half-apply)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-nc-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  // Move the tree so the patch no longer applies cleanly (git apply --check fails).
  fs.writeFileSync(path.join(repo, "foo.txt"), "diverged\n");
  const lines = [];
  try {
    withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "diverged\n",
      "must not apply when --check fails"
    );
    assert.ok(lines.some((l) => /apply --check failed/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop review: retains patch, does not apply", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-rev-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const lines = [];
  try {
    withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "review", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "review must not apply to the tree"
    );
    assert.ok(lines.some((l) => /retained|not applied/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
