// plugin/scripts/tests/peer-integrate.test.mjs
//
// Task 7.4 remediation: maybeIntegratePeerStop apply path. Real temp git target
// so apply / --check refusal / review-retain are exercised on disk. The
// --check-refusal case proves no half-apply; the reverse-on-failure branch
// mirrors the proven applyVerifiedPatch reverse (auto path is covered in
// integrate-auto.test.mjs).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  integratePeerStopFailClosed,
  maybeIntegratePeerStop,
  parseNumstatPaths,
  pathsFromGitPatch,
  peerStopExitCode,
} from "../lib/integrate.mjs";
import { Worker } from "node:worker_threads";
import { fileURLToPath } from "node:url";

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
  const runDir = path.join(xdg, "grok-skills", "runs", runId);
  const art = path.join(runDir, "artifacts");
  fs.mkdirSync(art, { recursive: true });
  const buf = Buffer.from(body);
  fs.writeFileSync(path.join(art, "implementation.patch"), buf);
  // Matching validation manifest: the apply path re-verifies patch bytes/sha256.
  const sha = createHash("sha256").update(buf).digest("hex");
  fs.writeFileSync(
    path.join(runDir, "implementation-handoff.json"),
    JSON.stringify({
      patch: { sha256: sha, bytes: buf.length, relativePath: "artifacts/implementation.patch" },
    })
  );
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

test("peer-stop auto: dirty patch-path blocks apply, tree unchanged, outcome !ok", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-nc-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  // foo.txt is both dirty AND the patch target -> the dirty-overlap guard blocks.
  fs.writeFileSync(path.join(repo, "foo.txt"), "diverged\n");
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "diverged\n",
      "must not apply when a patch path is already dirty"
    );
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false, "a blocked apply must report !ok so the command fails");
    assert.equal(res.outcome, "blocked-dirty-overlap");
    assert.ok(lines.some((l) => /dirty|overlap/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop direct: apply keys on the peer's repository, not companion cwd", () => {
  // Started for repoB; stopped from repoA (cwd) with no --target. Direct must
  // apply into repoB (envelope repository), never repoA (2.0.1+: no consent gate).
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-target-"));
  const repoA = initRepo(path.join(root, "repoA"));
  const repoB = initRepo(path.join(root, "repoB"));
  const xdg = path.join(root, "xdg");
  const patch = capturePatch(repoB);
  stagePatch(xdg, RUN_ID, patch);
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repoB), repoA, "direct", [], (l) => lines.push(l))
    );
    assert.equal(res.ok, true, JSON.stringify(res));
    assert.equal(res.attempted, true);
    assert.notEqual(res.outcome, "consent-required");
    // repoB received the patch; repoA (cwd) stays base.
    assert.notEqual(
      fs.readFileSync(path.join(repoB, "foo.txt"), "utf8"),
      "hello\n",
      "repoB must receive the apply"
    );
    assert.equal(
      fs.readFileSync(path.join(repoA, "foo.txt"), "utf8"),
      "hello\n",
      "repoA (cwd) must not be the apply target"
    );
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

test("integratePeerStopFailClosed converts an apply-path throw into attempted+not-ok", () => {
  // A non-string `cwd` makes resolveTargetWorkspaceRoot's path.resolve throw on a
  // READY envelope. The wrapper must CATCH it and fail closed, never propagate.
  const lines = [];
  const res = integratePeerStopFailClosed(
    peerStopEnvelope("relative-repo"), // relative -> path.resolve uses cwd
    123, // non-string cwd -> path.resolve throws inside maybeIntegratePeerStop
    "auto",
    [],
    (l) => lines.push(l)
  );
  assert.equal(res.attempted, true);
  assert.equal(res.ok, false);
  assert.equal(res.outcome, "integration-error");
  assert.equal(peerStopExitCode(0, res), 1);
});

test("peer-stop: a malformed patch (numstat fails) blocks fail-closed", () => {
  // If `git apply --numstat` fails we can't compute dirty overlap, and apply
  // --check alone can pass on a dirty file -> must block, never apply blind.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-numstat-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, "this is not a valid git patch\n"); // manifest matches this body
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(res.outcome, "blocked-numstat");
    assert.equal(res.ok, false);
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop: blocks (fail-closed) when pre-apply git status fails nonzero", () => {
  // A failed/killed `git status` yields an incomplete dirty set. `git apply`
  // (and --check/--numstat) does not require a repo, so a non-git target with
  // matching file bytes would still apply if we ignore the status failure.
  // Simulate unreadable status with a non-git repository path (status exits 128)
  // and assert blocked-dirty-status, !ok, nonzero exit, target bytes unchanged.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-nostatus-"));
  const donor = initRepo(path.join(root, "donor"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(donor));
  // Non-git target with the same base file content the patch expects.
  const notARepo = path.join(root, "not-a-repo");
  fs.mkdirSync(notARepo);
  fs.writeFileSync(path.join(notARepo, "foo.txt"), "hello\n");
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(
        peerStopEnvelope(notARepo),
        notARepo,
        "auto",
        ["--target", notARepo],
        (l) => lines.push(l)
      )
    );
    assert.equal(res.outcome, "blocked-dirty-status");
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false, "status failure must report !ok so the command fails");
    assert.equal(peerStopExitCode(0, res), 1);
    assert.equal(
      fs.readFileSync(path.join(notARepo, "foo.txt"), "utf8"),
      "hello\n",
      "target must be untouched when git status fails"
    );
    assert.ok(lines.some((l) => /status failed|dirty status|BLOCKED/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop: a patch tampered after validation is refused (integrity check)", () => {
  // The manifest records the ORIGINAL sha/bytes; if the patch on disk is swapped
  // after peer-stop validation, the companion apply must fail closed, not apply.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-integ-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo)); // writes patch + matching manifest
  // Tamper: overwrite the staged patch with different bytes (manifest now stale).
  const patchFile = path.join(xdg, "grok-skills", "runs", RUN_ID, "artifacts", "implementation.patch");
  fs.writeFileSync(patchFile, `${fs.readFileSync(patchFile, "utf8")}\n# injected\n`);
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(res.outcome, "patch-integrity-failure");
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false);
    assert.equal(peerStopExitCode(0, res), 1);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "a tampered patch must not be applied"
    );
    assert.ok(lines.some((l) => /integrity/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop: --target that differs from the peer repo is refused (no cross-repo apply)", () => {
  // SECURITY: consent for repo A (named via --target) must NOT authorize applying
  // the peer patch to repo B (the envelope repository). The patch belongs to the
  // peer's own repo, so a mismatched --target fails closed and nothing is applied.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-xrepo-"));
  const repoA = initRepo(path.join(root, "repoA")); // consented, named via --target
  const repoB = initRepo(path.join(root, "repoB")); // the peer's actual repo
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repoB));
  const lines = [];
  try {
    // --target names repoA but the envelope repository is repoB. The mismatch is
    // caught BEFORE any consent lookup, so even a consented repoA cannot launder
    // an apply onto repoB: it fails closed with no apply.
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repoB), repoA, "direct", ["--target", repoA], (l) =>
        lines.push(l)
      )
    );
    assert.equal(res.outcome, "target-mismatch");
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false);
    assert.equal(peerStopExitCode(0, res), 1);
    assert.equal(
      fs.readFileSync(path.join(repoB, "foo.txt"), "utf8"),
      "hello\n",
      "repoB (the peer repo) must be untouched"
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

function pureRenamePatchText() {
  return [
    "diff --git a/old.txt b/new.txt",
    "similarity index 100%",
    "rename from old.txt",
    "rename to new.txt",
    "",
  ].join("\n");
}

test("unit: pathsFromGitPatch includes both rename header sides (pure rename)", () => {
  const paths = pathsFromGitPatch(Buffer.from(pureRenamePatchText(), "utf8"));
  assert.ok(paths.has("old.txt"), "source side must be in touch set");
  assert.ok(paths.has("new.txt"), "destination side must be in touch set");
});

test("unit: pure-rename numstat is destination-only (documents the bug class)", () => {
  // Live git apply --numstat for 100% rename reports only the destination.
  assert.deepEqual(parseNumstatPaths("0\t0\tnew.txt\n"), ["new.txt"]);
  assert.ok(!parseNumstatPaths("0\t0\tnew.txt\n").includes("old.txt"));
});

test("peer-stop auto: pure rename + dirty SOURCE blocks apply (not destination-only)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-rename-"));
  const repo = path.join(root, "repo");
  fs.mkdirSync(repo, { recursive: true });
  git(repo, ["init"]);
  git(repo, ["config", "user.email", "t@t.t"]);
  git(repo, ["config", "user.name", "T"]);
  fs.writeFileSync(path.join(repo, "old.txt"), "hello\n");
  git(repo, ["add", "-A"]);
  git(repo, ["commit", "-m", "init"]);
  const xdg = path.join(root, "xdg");
  // Stage a real format-patch pure rename so git apply --check/--numstat accept it.
  git(repo, ["mv", "old.txt", "new.txt"]);
  git(repo, ["commit", "-m", "ren"]);
  const patchBody = spawnSync("git", ["format-patch", "-1", "--stdout"], {
    cwd: repo,
    encoding: "utf8",
  });
  assert.equal(patchBody.status, 0, patchBody.stderr);
  // Restore pre-rename tree so apply can land (or be blocked by dirty source).
  git(repo, ["checkout", "HEAD~1"]);
  stagePatch(xdg, RUN_ID, patchBody.stdout);
  fs.writeFileSync(path.join(repo, "old.txt"), "operator-dirty\n");
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false, "dirty rename SOURCE must block apply");
    assert.equal(res.outcome, "blocked-dirty-overlap");
    assert.equal(
      fs.readFileSync(path.join(repo, "old.txt"), "utf8"),
      "operator-dirty\n",
      "must not apply over a dirty rename source"
    );
    assert.ok(!fs.existsSync(path.join(repo, "new.txt")), "destination must not appear");
    assert.ok(lines.some((l) => /dirty|overlap|old\.txt/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop sequential restop: second apply is already-applied (idempotent)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-restop-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const lines = [];
  try {
    const first = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(first.ok, true);
    assert.equal(first.outcome, "applied");
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
    const second = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(second.attempted, true);
    assert.equal(second.ok, true, "restop must be idempotent success");
    assert.equal(second.outcome, "already-applied");
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop concurrent dual apply: one winner, no reverse of winner", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-dual-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const barrier = new Int32Array(new SharedArrayBuffer(4));
  const integratePath = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../lib/integrate.mjs"
  );
  const workerFile = path.join(root, "worker.mjs");
  fs.writeFileSync(
    workerFile,
    `import { parentPort, workerData } from "node:worker_threads";
import { pathToFileURL } from "node:url";
const view = new Int32Array(workerData.barrier);
Atomics.add(view, 0, 1);
while (Atomics.load(view, 0) < 2) {
  // spin until both workers arrive
}
const prev = process.env.XDG_STATE_HOME;
process.env.XDG_STATE_HOME = workerData.xdg;
try {
  const { maybeIntegratePeerStop } = await import(pathToFileURL(workerData.integratePath).href);
  const res = maybeIntegratePeerStop(
    workerData.envelope,
    workerData.repo,
    "auto",
    ["--target", workerData.repo],
    () => {}
  );
  parentPort.postMessage(res);
} catch (err) {
  parentPort.postMessage({ error: String((err && err.stack) || err) });
} finally {
  if (prev === undefined) delete process.env.XDG_STATE_HOME;
  else process.env.XDG_STATE_HOME = prev;
}
`
  );
  const envelope = peerStopEnvelope(repo);
  const mkWorker = () =>
    new Promise((resolve, reject) => {
      const w = new Worker(workerFile, {
        workerData: {
          barrier: barrier.buffer,
          integratePath,
          envelope,
          repo,
          xdg,
        },
      });
      w.on("message", resolve);
      w.on("error", reject);
      w.on("exit", (code) => {
        if (code !== 0) reject(new Error(`worker exit ${code}`));
      });
    });
  try {
    const results = await Promise.all([mkWorker(), mkWorker()]);
    for (const r of results) {
      assert.ok(!r.error, r.error);
      assert.equal(r.attempted, true);
      assert.equal(r.ok, true, JSON.stringify(r));
    }
    const outcomes = results.map((r) => r.outcome).sort();
    assert.deepEqual(outcomes, ["already-applied", "applied"]);
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop restop: applied marker exists but operator reverted patch - not already-applied", () => {
  // Marker alone is not durable proof: if the operator reverts the patch, restop
  // must re-apply (or re-block), never claim already-applied over a reverted tree.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-revert-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const lines = [];
  try {
    const first = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(first.ok, true);
    assert.equal(first.outcome, "applied");
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
    // Operator reverts the applied patch (checkout original content).
    fs.writeFileSync(path.join(repo, "foo.txt"), "hello\n");
    git(repo, ["add", "-A"]);
    // Clean tree matching pre-apply; marker still claims applied.
    const second = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(second.attempted, true);
    assert.notEqual(
      second.outcome,
      "already-applied",
      "reverted tree must not claim already-applied from a stale marker"
    );
    assert.equal(second.ok, true, JSON.stringify(second));
    assert.equal(second.outcome, "applied");
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("apply lock: live holder and abandoned dead lock are never stolen", async () => {
  const {
    acquireApplyLock,
    isApplyLockReclaimable,
    tryReclaimLockDir,
    targetIdentityKey,
  } = await import("../lib/integrate-apply-state.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-apply-lock-"));
  const xdg = path.join(root, "xdg");
  const prev = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  const runId = RUN_ID;
  const targetKey = targetIdentityKey(root);
  try {
    // Live holder: second acquire must wait/timeout without reclaiming.
    const releaseLive = acquireApplyLock(runId, targetKey, process.env, 200);
    let stole = false;
    try {
      acquireApplyLock(runId, targetKey, process.env, 80);
      stole = true;
    } catch (err) {
      assert.match(String(err.message || err), /timeout/i);
    }
    assert.equal(stole, false, "must not steal a live holder's lock");
    releaseLive();

    // Abandoned dead owner: automatic reclaim is disabled - timeout + diagnostics;
    // lock remains until manual cleanup / holder release.
    const release2 = acquireApplyLock(runId, targetKey, process.env, 200);
    release2();
    const runsDir = path.join(xdg, "grok-skills", "runs", runId);
    const lockDir = path.join(runsDir, "apply-locks", `${targetKey}.lock`);
    fs.mkdirSync(lockDir, { recursive: true });
    fs.writeFileSync(
      path.join(lockDir, "owner.json"),
      `${JSON.stringify({
        schemaVersion: 1,
        pid: 999999991,
        startToken: "dead-token",
        acquiredAt: new Date(Date.now() - 60_000).toISOString(),
      })}\n`
    );
    assert.equal(isApplyLockReclaimable(lockDir, 1_000), true, "diagnostic looksAbandoned");
    assert.equal(tryReclaimLockDir(lockDir, 1_000), false, "reclaim always no-op");
    let reclaimedDead = false;
    let deadTimeoutMsg = "";
    try {
      acquireApplyLock(runId, targetKey, process.env, 80, { staleMs: 1_000 });
      reclaimedDead = true;
    } catch (err) {
      deadTimeoutMsg = String(err.message || err);
    }
    assert.equal(reclaimedDead, false, "dead abandoned lock must not be auto-reclaimed");
    assert.match(deadTimeoutMsg, /timeout/i);
    assert.match(deadTimeoutMsg, /automatic reclaim disabled|looksAbandoned|manual/i);
    assert.equal(
      JSON.parse(fs.readFileSync(path.join(lockDir, "owner.json"), "utf8")).startToken,
      "dead-token"
    );

    // Unknown owner: do not reclaim (fail closed; wait/timeout).
    fs.rmSync(lockDir, { recursive: true, force: true });
    fs.mkdirSync(lockDir, { recursive: true });
    fs.writeFileSync(
      path.join(lockDir, "owner.json"),
      `${JSON.stringify({
        schemaVersion: 1,
        // no pid - unknown
        acquiredAt: new Date().toISOString(),
      })}\n`
    );
    let reclaimedUnknownFresh = false;
    try {
      acquireApplyLock(runId, targetKey, process.env, 80, { staleMs: 60_000 });
      reclaimedUnknownFresh = true;
    } catch (err) {
      assert.match(String(err.message || err), /timeout/i);
    }
    assert.equal(reclaimedUnknownFresh, false, "unknown owner must not be stolen");
  } finally {
    if (prev === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prev;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop: marker persistence failure after apply cannot report durable applied success", async () => {
  const { targetIdentityKey, locateApplyMarker } = await import("../lib/integrate-apply-state.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-marker-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const lines = [];
  const prev = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  try {
    // Occupy the durable marker path with a DIRECTORY so atomic write/rename fails
    // after a successful git apply. Lock acquisition still works under runs/<id>.
    const targetKey = targetIdentityKey(repo);
    const markerPath = locateApplyMarker(RUN_ID, targetKey, process.env);
    assert.ok(markerPath);
    fs.mkdirSync(markerPath, { recursive: true });
    const res = maybeIntegratePeerStop(
      peerStopEnvelope(repo),
      repo,
      "auto",
      ["--target", repo],
      (l) => lines.push(l)
    );
    assert.equal(res.attempted, true);
    assert.notEqual(res.outcome, "applied", "must not claim applied without durable marker");
    assert.notEqual(res.outcome, "already-applied");
    assert.equal(res.ok, false, JSON.stringify(res));
    const body = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
    if (res.outcome === "marker-persist-failure") {
      assert.equal(body, "hello\n", "successful reverse after marker fail restores tree");
    } else {
      assert.equal(res.outcome, "manual-needed", JSON.stringify({ res, body, lines }));
    }
  } finally {
    if (prev === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prev;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop restop: crash after apply before marker heals durable marker as already-applied", () => {
  // Crash window: git apply succeeded, process died before writeApplyMarker.
  // reverse --check succeeds (tree has patch) but no marker exists. Restop must
  // heal the durable marker under lock and return already-applied BEFORE the
  // dirty-overlap guard (applied paths look dirty vs HEAD).
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-orphan-apply-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatch(repo);
  stagePatch(xdg, RUN_ID, patchBody);
  const lines = [];
  try {
    // Simulate crash-after-apply: land the patch with no marker write.
    const patchPath = path.join(
      xdg,
      "grok-skills",
      "runs",
      RUN_ID,
      "artifacts",
      "implementation.patch"
    );
    const applied = spawnSync("git", ["apply", "--binary", patchPath], {
      cwd: repo,
      encoding: "utf8",
    });
    assert.equal(applied.status, 0, applied.stderr);
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
    // reverse --check proves the tree still has the patch; no marker on disk.
    const revCheck = spawnSync("git", ["apply", "-R", "--check", "--binary", patchPath], {
      cwd: repo,
      encoding: "utf8",
    });
    assert.equal(revCheck.status, 0, "precondition: reverse --check must succeed");

    const second = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(second.attempted, true);
    assert.equal(second.ok, true, JSON.stringify({ second, lines }));
    assert.equal(
      second.outcome,
      "already-applied",
      "orphan applied tree must heal marker and report already-applied, not dirty-block"
    );
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
    assert.ok(
      lines.some((l) => /heal|already-applied/i.test(l)),
      lines.join("\n")
    );
    // Durable marker must now exist for a subsequent restop.
    const third = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(third.outcome, "already-applied");
    assert.equal(third.ok, true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop restop: marker-persist reverse-failure leaving applied tree heals on restop", async () => {
  // After marker-persist-failure, reverse can also fail and leave the applied
  // tree without a durable marker (manual-needed). A later restop must heal the
  // marker and return already-applied instead of dirty-overlap blocking.
  const {
    targetIdentityKey,
    locateApplyMarker,
    clearApplyMarker,
  } = await import("../lib/integrate-apply-state.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-persist-orphan-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repo));
  const lines = [];
  try {
    // First apply succeeds and writes a marker.
    const first = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(first.ok, true);
    assert.equal(first.outcome, "applied");
    // Simulate marker-persist reverse-failure residue: applied tree, no marker.
    clearApplyMarker(RUN_ID, targetIdentityKey(repo), { XDG_STATE_HOME: xdg });
    const markerPath = locateApplyMarker(RUN_ID, targetIdentityKey(repo), {
      XDG_STATE_HOME: xdg,
    });
    assert.ok(markerPath);
    assert.equal(fs.existsSync(markerPath), false, "precondition: marker cleared");
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");

    const second = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(second.attempted, true);
    assert.equal(second.ok, true, JSON.stringify({ second, lines }));
    assert.equal(second.outcome, "already-applied");
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");
    assert.equal(fs.existsSync(markerPath), true, "heal must write durable marker");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("unit: loadPatchTouchPaths fails closed when patch header read fails after numstat", async () => {
  const { loadPatchTouchPaths } = await import("../lib/integrate.mjs");
  const res = loadPatchTouchPaths(
    path.join(os.tmpdir(), `grok-missing-patch-${process.pid}-${Date.now()}.patch`),
    "1\t1\tfoo.txt\n"
  );
  assert.equal(res.ok, false);
  assert.equal(res.outcome, "blocked-patch-headers");
  assert.match(String(res.reason || ""), /header|numstat|touch/i);
});
