// plugin/scripts/tests/peer-integrate-review-blockers.test.mjs
//
// Residual review blockers for peer/auto integrate: acquireApplyLock owner
// durability + never ownerless age reclaim; loadPatchTouchPaths header
// fail-closed; heal path revalidateUnderLock before marker write.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

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
  const sha = createHash("sha256").update(buf).digest("hex");
  fs.writeFileSync(
    path.join(runDir, "implementation-handoff.json"),
    JSON.stringify({
      patch: { sha256: sha, bytes: buf.length, relativePath: "artifacts/implementation.patch" },
    })
  );
}

function pureRenamePatchText() {
  return [
    "diff --git a/old.txt b/new.txt",
    "similarity index 100%",
    "rename from old.txt",
    "rename to new.txt",
    "",
  ].join("\n");
}



test("unit: loadPatchTouchPaths fails closed when headers empty/unparseable for non-empty numstat", async () => {
  const { loadPatchTouchPaths } = await import("../lib/integrate.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-touch-empty-hdr-"));
  try {
    // Non-empty numstat but no load-bearing diff --git / rename-copy headers.
    const emptyHdr = path.join(root, "empty-headers.patch");
    fs.writeFileSync(emptyHdr, "Index: foo.txt\n=== not a git diff header set ===\n+foo\n");
    const empty = loadPatchTouchPaths(emptyHdr, "1\t1\tfoo.txt\n");
    assert.equal(empty.ok, false, "must not fall back to numstat-only once headers are load-bearing");
    assert.equal(empty.outcome, "blocked-patch-headers");
    assert.match(String(empty.reason || ""), /header|empty|unparseable|touch/i);

    // Malformed/nonstandard diff --git that parseDiffGitHeaderPaths cannot pair.
    const badHdr = path.join(root, "malformed-headers.patch");
    fs.writeFileSync(
      badHdr,
      "diff --git a/only-one-side\n" +
        "--- a/only-one-side\n+++ b/only-one-side\n@@ -1 +1 @@\n-a\n+b\n"
    );
    const bad = loadPatchTouchPaths(badHdr, "1\t1\tonly-one-side\n");
    assert.equal(bad.ok, false, "malformed headers must fail closed (no numstat-only)");
    assert.equal(bad.outcome, "blocked-patch-headers");

    // Pure rename stays green: destination-biased numstat + headers corroborate both sides.
    const rename = path.join(root, "pure-rename.patch");
    fs.writeFileSync(rename, pureRenamePatchText());
    const good = loadPatchTouchPaths(rename, "0\t0\tnew.txt\n");
    assert.equal(good.ok, true, JSON.stringify(good));
    assert.ok(good.paths.includes("old.txt"), "source from headers");
    assert.ok(good.paths.includes("new.txt"), "destination from numstat/headers");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("unit: loadPatchTouchPaths fails closed when rename destination cannot be corroborated from headers", async () => {
  const { loadPatchTouchPaths } = await import("../lib/integrate.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-touch-rename-gap-"));
  try {
    // Numstat claims a rename destination that headers never mention.
    const pth = path.join(root, "gap.patch");
    fs.writeFileSync(
      pth,
      [
        "diff --git a/old.txt b/old.txt",
        "--- a/old.txt",
        "+++ b/old.txt",
        "@@ -1 +1 @@",
        "-a",
        "+b",
        "",
      ].join("\n")
    );
    const res = loadPatchTouchPaths(pth, "0\t0\tnew.txt\n");
    assert.equal(res.ok, false, "numstat destination missing from headers must fail closed");
    assert.equal(res.outcome, "blocked-patch-headers");
    assert.match(String(res.reason || ""), /corroborat|header|rename|destination|touch/i);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("apply lock: owner-write failure removes mkdir; ownerless never age-reclaims", async () => {
  const {
    acquireApplyLock,
    isApplyLockReclaimable,
    targetIdentityKey,
  } = await import("../lib/integrate-apply-state.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-apply-lock-owner-write-"));
  const xdg = path.join(root, "xdg");
  const prev = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  const runId = RUN_ID;
  const targetKey = targetIdentityKey(root);
  const runsDir = path.join(xdg, "grok-skills", "runs", runId);
  const lockDir = path.join(runsDir, "apply-locks", `${targetKey}.lock`);
  try {
    // 1) Ownerless/unknown lock must NEVER reclaim on age alone.
    fs.mkdirSync(lockDir, { recursive: true });
    const old = new Date(Date.now() - 120_000);
    fs.utimesSync(lockDir, old, old);
    assert.equal(
      isApplyLockReclaimable(lockDir, 1_000),
      false,
      "ownerless/unknown lock must never reclaim on age alone"
    );
    let stoleOwnerless = false;
    try {
      acquireApplyLock(runId, targetKey, process.env, 80, { staleMs: 1_000 });
      stoleOwnerless = true;
    } catch (err) {
      assert.match(String(err.message || err), /timeout/i);
    }
    assert.equal(stoleOwnerless, false, "must not reclaim ownerless lock on age");
    fs.rmSync(lockDir, { recursive: true, force: true });

    // 2) Unreadable owner (owner.json is a directory) is unknown - not age-reclaimable.
    fs.mkdirSync(lockDir, { recursive: true });
    fs.mkdirSync(path.join(lockDir, "owner.json"), { recursive: true });
    fs.utimesSync(lockDir, old, old);
    assert.equal(
      isApplyLockReclaimable(lockDir, 1_000),
      false,
      "unreadable owner must not age-reclaim"
    );
    let stoleBroken = false;
    try {
      acquireApplyLock(runId, targetKey, process.env, 80, { staleMs: 1_000 });
      stoleBroken = true;
    } catch (err) {
      assert.match(String(err.message || err), /timeout/i);
    }
    assert.equal(stoleBroken, false, "broken owner record must not be age-reclaimed");
    fs.rmSync(lockDir, { recursive: true, force: true });

    // 3) Owner-write failure after mkdir: fail closed, remove lockDir, do not return release.
    // Patch the shared default `fs` binding (same module instance product code imports).
    const realWrite = fs.writeFileSync;
    let tripped = false;
    fs.writeFileSync = function patchedWrite(file, data, opts) {
      const s = String(file);
      if (s.endsWith(`${path.sep}owner.json`) || s.endsWith("/owner.json")) {
        tripped = true;
        const e = new Error("EIO forced owner write failure");
        e.code = "EIO";
        throw e;
      }
      return realWrite.call(this, file, data, opts);
    };
    let acquiredAfterFail = false;
    let errMsg = "";
    try {
      try {
        acquireApplyLock(runId, targetKey, process.env, 500, { staleMs: 1_000 });
        acquiredAfterFail = true;
      } catch (err) {
        errMsg = String(err.message || err);
      }
      assert.equal(tripped, true, "precondition: owner write must have been attempted");
      assert.equal(acquiredAfterFail, false, "must not return release when owner write fails");
      assert.match(errMsg, /owner|lock|fail|closed|durable|write/i, errMsg);
      assert.equal(
        fs.existsSync(lockDir),
        false,
        "mkdir must be removed when owner.json cannot be written"
      );
    } finally {
      fs.writeFileSync = realWrite;
    }

    // 4) Positive dead identity still reclaims; durable owner is re-readable as this pid.
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
    const releaseDead = acquireApplyLock(runId, targetKey, process.env, 500, {
      staleMs: 1_000,
    });
    assert.equal(typeof releaseDead, "function");
    assert.equal(fs.existsSync(path.join(lockDir, "owner.json")), true);
    const owner = JSON.parse(fs.readFileSync(path.join(lockDir, "owner.json"), "utf8"));
    assert.equal(owner.pid, process.pid);
    releaseDead();
  } finally {
    if (prev === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prev;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("heal/already-applied: revalidateUnderLock before marker on orphan apply (wrong-sha)", async () => {
  // Crash-after-apply heal must call revalidateUnderLock before writing the durable
  // marker. Wrong-sha / tamper fails closed without claiming already-applied.
  const {
    completeIntegrationApplyUnderLock,
    verifyPatchAgainstManifest,
    targetIdentityKey,
    locateApplyMarker,
  } = await import("../lib/integrate.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-heal-reval-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatch(repo);
  stagePatch(xdg, RUN_ID, patchBody);
  const patchPath = path.join(
    xdg,
    "grok-skills",
    "runs",
    RUN_ID,
    "artifacts",
    "implementation.patch"
  );
  const lines = [];
  const prev = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  try {
    const applied = spawnSync("git", ["apply", "--binary", patchPath], {
      cwd: repo,
      encoding: "utf8",
    });
    assert.equal(applied.status, 0, applied.stderr);
    const honestSha = createHash("sha256").update(fs.readFileSync(patchPath)).digest("hex");
    const targetKey = targetIdentityKey(repo);
    const markerPath = locateApplyMarker(RUN_ID, targetKey, process.env);
    assert.equal(fs.existsSync(markerPath), false);

    let revalidateCalls = 0;
    const res = completeIntegrationApplyUnderLock({
      targetRepo: repo,
      patchPath,
      runId: RUN_ID,
      targetKey,
      patchSha: honestSha,
      env: process.env,
      stderrLine: (l) => lines.push(l),
      logTag: "grok-peer",
      revalidateUnderLock: () => {
        revalidateCalls += 1;
        return {
          ok: false,
          outcome: "patch-integrity-failure",
          reason: "patch sha256 does not match manifest",
          runId: RUN_ID,
          patchPath,
          patchSha: honestSha,
        };
      },
    });
    assert.equal(revalidateCalls, 1, "heal path must call revalidateUnderLock before marker");
    assert.equal(res.ok, false, JSON.stringify({ res, lines }));
    assert.equal(res.outcome, "patch-integrity-failure");
    assert.equal(
      fs.existsSync(markerPath),
      false,
      "must not write durable marker when under-lock revalidation fails"
    );
    assert.equal(fs.readFileSync(path.join(repo, "foo.txt"), "utf8"), "hello world\n");

    // Control: honest revalidate still heals (shared under-lock ladder remains shared).
    const res2 = completeIntegrationApplyUnderLock({
      targetRepo: repo,
      patchPath,
      runId: RUN_ID,
      targetKey,
      patchSha: honestSha,
      env: process.env,
      stderrLine: (l) => lines.push(l),
      logTag: "grok-peer",
      revalidateUnderLock: () => {
        const v = verifyPatchAgainstManifest(RUN_ID, patchPath, process.env);
        if (!v.ok) {
          return {
            ok: false,
            outcome: "patch-integrity-failure",
            reason: v.reason,
            runId: RUN_ID,
            patchPath,
            patchSha: honestSha,
          };
        }
        return { ok: true };
      },
    });
    assert.equal(res2.ok, true, JSON.stringify({ res2, lines }));
    assert.equal(res2.outcome, "already-applied");
    assert.equal(fs.existsSync(markerPath), true, "honest revalidate may heal marker");
  } finally {
    if (prev === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prev;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
