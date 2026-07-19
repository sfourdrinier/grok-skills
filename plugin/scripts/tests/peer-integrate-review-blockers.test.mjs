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

    // 4) Positive dead identity is diagnostic-only; acquire does NOT reclaim it.
    // After manual cleanup, a fresh acquire writes durable self owner.
    fs.mkdirSync(lockDir, { recursive: true });
    const deadBody = {
      schemaVersion: 1,
      pid: 999999991,
      startToken: "dead-token",
      acquiredAt: new Date(Date.now() - 60_000).toISOString(),
    };
    fs.writeFileSync(path.join(lockDir, "owner.json"), `${JSON.stringify(deadBody)}\n`);
    assert.equal(
      isApplyLockReclaimable(lockDir, 1_000),
      true,
      "dead+stale looks abandoned (diagnostic)"
    );
    let stoleDead = false;
    let deadErr = "";
    try {
      acquireApplyLock(runId, targetKey, process.env, 80, { staleMs: 1_000 });
      stoleDead = true;
    } catch (err) {
      deadErr = String(err.message || err);
    }
    assert.equal(stoleDead, false, "must not reclaim dead lock automatically");
    assert.match(deadErr, /timeout/i);
    assert.match(deadErr, /automatic reclaim disabled|looksAbandoned|manual/i, deadErr);
    const stillDead = JSON.parse(
      fs.readFileSync(path.join(lockDir, "owner.json"), "utf8")
    );
    assert.equal(stillDead.pid, 999999991);
    assert.equal(stillDead.startToken, "dead-token");
    // Manual cleanup then acquire succeeds with durable self owner.
    fs.rmSync(lockDir, { recursive: true, force: true });
    const release = acquireApplyLock(runId, targetKey, process.env, 500);
    assert.equal(typeof release, "function");
    const owner = JSON.parse(fs.readFileSync(path.join(lockDir, "owner.json"), "utf8"));
    assert.equal(owner.pid, process.pid);
    release();
  } finally {
    if (prev === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prev;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("apply lock: no automatic reclaim under 3-contender race (fail closed)", async () => {
  // Three contenders on one lock name:
  //   D - dead abandoned owner already on disk
  //   A - stale reclaimer/acquirer that would previously rename D (or B) away
  //   B - fresh live replacement
  //   C - third acquirer after any displacement
  // Protocol must never rename/delete an existing lockDir; A and C time out;
  // B remains sole holder; no contender can release another's lock.
  const {
    acquireApplyLock,
    tryReclaimLockDir,
    isApplyLockReclaimable,
    formatApplyLockDiag,
    targetIdentityKey,
  } = await import("../lib/integrate-apply-state.mjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-apply-lock-3race-"));
  const xdg = path.join(root, "xdg");
  const prev = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  const runId = RUN_ID;
  const targetKey = targetIdentityKey(root);
  const runsDir = path.join(xdg, "grok-skills", "runs", runId);
  const lockDir = path.join(runsDir, "apply-locks", `${targetKey}.lock`);
  const realRename = fs.renameSync;
  const realRm = fs.rmSync;
  let renameOnLock = 0;
  let rmOnLock = 0;
  try {
    fs.mkdirSync(lockDir, { recursive: true });
    const deadOwner = {
      schemaVersion: 1,
      pid: 999999991,
      startToken: "dead-token-stale",
      acquiredAt: new Date(Date.now() - 60_000).toISOString(),
    };
    fs.writeFileSync(path.join(lockDir, "owner.json"), `${JSON.stringify(deadOwner)}\n`);
    assert.equal(isApplyLockReclaimable(lockDir, 1_000), true);

    fs.renameSync = function patchedRename(from, to) {
      if (String(from) === lockDir || String(to) === lockDir) renameOnLock += 1;
      return realRename.call(this, from, to);
    };
    fs.rmSync = function patchedRm(p, opts) {
      if (String(p) === lockDir) rmOnLock += 1;
      return realRm.call(this, p, opts);
    };

    // Contender A: tryReclaim is a permanent no-op (never renames/deletes).
    assert.equal(tryReclaimLockDir(lockDir, 1_000), false);
    let aAcquired = false;
    let aErr = "";
    try {
      acquireApplyLock(runId, targetKey, process.env, 80, { staleMs: 1_000 });
      aAcquired = true;
    } catch (err) {
      aErr = String(err.message || err);
    }
    assert.equal(aAcquired, false, "A must not acquire over dead D");
    assert.match(aErr, /timeout/i);
    assert.match(aErr, /automatic reclaim disabled|looksAbandoned|manual/i, aErr);
    assert.equal(renameOnLock, 0, "A must never rename the lock dir");
    assert.equal(rmOnLock, 0, "A must never rm the lock dir on reclaim path");
    assert.equal(
      JSON.parse(fs.readFileSync(path.join(lockDir, "owner.json"), "utf8")).startToken,
      "dead-token-stale",
      "D must remain undisturbed"
    );
    assert.match(formatApplyLockDiag(lockDir), /looksAbandoned=yes|automatic reclaim disabled/i);

    // Contender B: operator-style replacement (or a prior legitimate release+acquire)
    // becomes the sole live holder under the exclusive name.
    fs.rmSync = realRm;
    fs.renameSync = realRename;
    fs.rmSync(lockDir, { recursive: true, force: true });
    const releaseB = acquireApplyLock(runId, targetKey, process.env, 500);
    assert.equal(typeof releaseB, "function");
    const bOwnerBefore = JSON.parse(
      fs.readFileSync(path.join(lockDir, "owner.json"), "utf8")
    );
    assert.equal(bOwnerBefore.pid, process.pid);

    // Contender C: concurrent acquire against live B - timeout, B intact.
    renameOnLock = 0;
    rmOnLock = 0;
    fs.renameSync = function patchedRename(from, to) {
      if (String(from) === lockDir || String(to) === lockDir) renameOnLock += 1;
      return realRename.call(this, from, to);
    };
    fs.rmSync = function patchedRm(p, opts) {
      if (String(p) === lockDir) rmOnLock += 1;
      return realRm.call(this, p, opts);
    };
    let cAcquired = false;
    let cRelease = null;
    try {
      cRelease = acquireApplyLock(runId, targetKey, process.env, 80);
      cAcquired = true;
    } catch (err) {
      assert.match(String(err.message || err), /timeout/i);
    }
    assert.equal(cAcquired, false, "C must not acquire over live B");
    assert.equal(cRelease, null, "C must not obtain a release handle");
    assert.equal(renameOnLock, 0, "C must never rename B's lock");
    assert.equal(rmOnLock, 0, "C must never rm B's lock");
    const bOwnerAfter = JSON.parse(
      fs.readFileSync(path.join(lockDir, "owner.json"), "utf8")
    );
    assert.equal(bOwnerAfter.pid, bOwnerBefore.pid);
    assert.equal(bOwnerAfter.startToken, bOwnerBefore.startToken);
    assert.equal(bOwnerAfter.acquiredAt, bOwnerBefore.acquiredAt);

    // Only B may release; after B releases, a new acquire can succeed.
    fs.renameSync = realRename;
    fs.rmSync = realRm;
    releaseB();
    assert.equal(fs.existsSync(lockDir), false);
    const releaseAfter = acquireApplyLock(runId, targetKey, process.env, 500);
    assert.equal(typeof releaseAfter, "function");
    releaseAfter();
  } finally {
    fs.renameSync = realRename;
    fs.rmSync = realRm;
    if (prev === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = prev;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("verifyPatchAgainstManifest: post-stat hash/read failure is structured (not throw)", async () => {
  const { verifyPatchAgainstManifest, applyVerifiedPatch } = await import(
    "../lib/integrate.mjs"
  );
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-patch-stat-race-"));
  const xdg = path.join(root, "xdg");
  const repo = initRepo(path.join(root, "repo"));
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
  const prev = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = xdg;
  const realRead = fs.readFileSync;
  const realStat = fs.statSync;
  try {
    // After a successful size stat, the next read of the patch fails (unlink/EIO).
    let sawPatchStat = false;
    fs.statSync = function patchedStat(p, ...rest) {
      const st = realStat.call(this, p, ...rest);
      if (String(p) === patchPath) sawPatchStat = true;
      return st;
    };
    fs.readFileSync = function patchedRead(p, ...rest) {
      if (String(p) === patchPath && sawPatchStat) {
        const e = new Error("ENOENT injected post-stat");
        e.code = "ENOENT";
        throw e;
      }
      return realRead.call(this, p, ...rest);
    };

    let threw = false;
    let result;
    try {
      result = verifyPatchAgainstManifest(RUN_ID, patchPath, process.env);
    } catch {
      threw = true;
    }
    assert.equal(threw, false, "must not throw on post-stat hash failure");
    assert.equal(result?.ok, false);
    assert.equal(result?.reason, "patch unreadable");

    // Auto apply path: pre-verify hash race is blocked-patch-unreadable; either
    // structured blocked outcome must finalize (never throw).
    sawPatchStat = false;
    let applyThrew = false;
    let unit;
    try {
      unit = applyVerifiedPatch({
        wrapper: "unused",
        runId: RUN_ID,
        targetRepo: repo,
        env: process.env,
        runHandoff: () => ({
          code: 0,
          envelope: { response: { integration: { ready: true } } },
        }),
        stderrLine: () => {},
      });
    } catch {
      applyThrew = true;
    }
    assert.equal(applyThrew, false, "applyVerifiedPatch must not throw on hash race");
    assert.equal(unit?.ok, false, JSON.stringify(unit));
    assert.ok(
      unit?.outcome === "blocked-patch-unreadable" ||
        unit?.outcome === "patch-integrity-failure",
      `expected structured blocked outcome, got ${unit?.outcome}`
    );
  } finally {
    fs.readFileSync = realRead;
    fs.statSync = realStat;
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
