// plugin/scripts/tests/apply-protected-path.test.mjs
//
// TDD: shared auto/peer apply spine must pre-block deny-listed touch paths
// (blocked-protected-path) before git apply --check/apply; tree unchanged.
// Full touch set = numstat union diff/rename headers (rename source/dest).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  applyVerifiedPatch,
  maybeIntegratePeerStop,
  peerStopExitCode,
} from "../lib/integrate.mjs";

// Must match progress-relay RUN_ID_RE: YYYYMMDDTHHMMSSZ- + 6 hex.
const RUN_ID = "20260718T120000Z-abcd01";

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
  fs.writeFileSync(path.join(dir, "safe.txt"), "safe\n");
  fs.writeFileSync(path.join(dir, ".env"), "SECRET=keep\n");
  fs.writeFileSync(path.join(dir, ".env.local"), "LOCAL=1\n");
  fs.mkdirSync(path.join(dir, "keys"), { recursive: true });
  fs.writeFileSync(path.join(dir, "keys", "id_rsa"), "-----BEGIN FAKE-----\n");
  fs.writeFileSync(path.join(dir, "server.pem"), "-----BEGIN FAKE PEM-----\n");
  fs.writeFileSync(path.join(dir, "credentials.json"), '{"token":"x"}\n');
  git(dir, ["add", "-A"]);
  git(dir, ["add", "-f", ".env", ".env.local", "keys/id_rsa", "server.pem", "credentials.json"]);
  git(dir, ["commit", "-m", "init"]);
  return dir;
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

function capturePathPatch(repo, rel, newContent) {
  const abs = path.join(repo, rel);
  const original = fs.readFileSync(abs);
  fs.writeFileSync(abs, newContent);
  const d = spawnSync("git", ["diff", "--binary", "HEAD", "--", rel], {
    cwd: repo,
    encoding: "utf8",
  });
  assert.equal(d.status, 0, d.stderr);
  fs.writeFileSync(abs, original);
  assert.ok(d.stdout.length > 0, `expected patch for ${rel}`);
  return d.stdout;
}

function captureSafePatch(repo) {
  return capturePathPatch(repo, "safe.txt", "safe edited\n");
}

function captureMixedSafeAndEnv(repo) {
  const a = path.join(repo, "safe.txt");
  const b = path.join(repo, ".env");
  const oa = fs.readFileSync(a);
  const ob = fs.readFileSync(b);
  fs.writeFileSync(a, "safe mixed\n");
  fs.writeFileSync(b, "SECRET=leaked\n");
  const d = spawnSync("git", ["diff", "--binary", "HEAD"], { cwd: repo, encoding: "utf8" });
  assert.equal(d.status, 0, d.stderr);
  fs.writeFileSync(a, oa);
  fs.writeFileSync(b, ob);
  return d.stdout;
}

function captureRenamePatch(repo, fromRel, toRel) {
  git(repo, ["mv", fromRel, toRel]);
  const d = spawnSync("git", ["diff", "--binary", "--cached", "HEAD"], {
    cwd: repo,
    encoding: "utf8",
  });
  assert.equal(d.status, 0, d.stderr);
  git(repo, ["reset", "--hard", "HEAD"]);
  assert.ok(/rename from|diff --git/.test(d.stdout), d.stdout.slice(0, 200));
  return d.stdout;
}

function treeSnapshot(repo, rels) {
  const out = {};
  for (const r of rels) {
    const p = path.join(repo, r);
    out[r] = fs.existsSync(p) ? fs.readFileSync(p) : null;
  }
  return out;
}

function assertTreeUnchanged(repo, before, rels) {
  const after = treeSnapshot(repo, rels);
  for (const r of rels) {
    assert.deepEqual(after[r], before[r], `tree path changed: ${r}`);
  }
}

function assertBlockedProtected(res, { expectProtectedSubstring } = {}) {
  assert.equal(res.ok, false, `expected !ok, got ${JSON.stringify(res)}`);
  assert.equal(res.outcome, "blocked-protected-path", JSON.stringify(res));
  if (expectProtectedSubstring) {
    const list = res.protectedPaths || [];
    assert.ok(
      list.some((p) => p.includes(expectProtectedSubstring)),
      `expected protectedPaths to mention ${expectProtectedSubstring}, got ${JSON.stringify(list)}`
    );
  }
}

function autoApply(repo, xdg, runId = RUN_ID) {
  return applyVerifiedPatch({
    wrapper: "unused",
    runId,
    targetRepo: repo,
    runHandoff: () => ({ code: 0, envelope: { response: { integration: { ready: true } } } }),
    stderrLine: () => {},
    env: { XDG_STATE_HOME: xdg },
  });
}

test("auto: .env patch is blocked-protected-path; tree unchanged", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-env-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const before = treeSnapshot(repo, [".env", "safe.txt"]);
  stagePatch(xdg, RUN_ID, capturePathPatch(repo, ".env", "SECRET=leaked\n"));
  try {
    const res = autoApply(repo, xdg);
    assertBlockedProtected(res, { expectProtectedSubstring: ".env" });
    assertTreeUnchanged(repo, before, [".env", "safe.txt"]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: .env.local patch is blocked-protected-path", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-envl-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const before = treeSnapshot(repo, [".env.local"]);
  stagePatch(xdg, RUN_ID, capturePathPatch(repo, ".env.local", "LOCAL=2\n"));
  try {
    const res = autoApply(repo, xdg);
    assertBlockedProtected(res, { expectProtectedSubstring: ".env.local" });
    assertTreeUnchanged(repo, before, [".env.local"]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: .git/hooks nested + .git/index touch set is denied by pathMatchesDeny", async () => {
  // Pure match coverage for .git paths (git may refuse numstat on internal paths).
  const { pathMatchesDeny, protectedPathsIn } = await import("../lib/deny-write.mjs");
  assert.equal(pathMatchesDeny(".git/hooks/vendor/pre-commit"), true);
  assert.equal(pathMatchesDeny(".git/index"), true);
  assert.deepEqual(
    protectedPathsIn(["safe.txt", ".git/hooks/vendor/nested", ".git/index"]),
    [".git/hooks/vendor/nested", ".git/index"]
  );
});

test("auto: id key / pem / credentials patches block", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-keys-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const cases = [
    ["keys/id_rsa", "-----BEGIN FAKE-----\nLEAK\n", "20260718T120001Z-abcd01"],
    ["server.pem", "-----BEGIN FAKE PEM-----\nLEAK\n", "20260718T120002Z-abcd02"],
    ["credentials.json", '{"token":"leak"}\n', "20260718T120003Z-abcd03"],
  ];
  try {
    for (const [rel, content, runId] of cases) {
      stagePatch(xdg, runId, capturePathPatch(repo, rel, content));
      const before = fs.readFileSync(path.join(repo, rel));
      const res = autoApply(repo, xdg, runId);
      assertBlockedProtected(res);
      assert.deepEqual(fs.readFileSync(path.join(repo, rel)), before, rel);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: mixed safe + protected blocks entirely; safe not applied", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-mixed-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const before = treeSnapshot(repo, [".env", "safe.txt"]);
  stagePatch(xdg, RUN_ID, captureMixedSafeAndEnv(repo));
  try {
    const res = autoApply(repo, xdg);
    assertBlockedProtected(res, { expectProtectedSubstring: ".env" });
    assertTreeUnchanged(repo, before, [".env", "safe.txt"]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: rename safe -> protected blocks (destination in touch set)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-ren-sp-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  fs.unlinkSync(path.join(repo, "credentials.json"));
  git(repo, ["add", "-A"]);
  git(repo, ["commit", "-m", "drop creds for rename dest"]);
  const beforeSafe = fs.readFileSync(path.join(repo, "safe.txt"));
  stagePatch(xdg, RUN_ID, captureRenamePatch(repo, "safe.txt", "credentials.json"));
  try {
    const res = autoApply(repo, xdg);
    assertBlockedProtected(res, { expectProtectedSubstring: "credentials.json" });
    assert.equal(fs.readFileSync(path.join(repo, "safe.txt"), "utf8"), beforeSafe.toString("utf8"));
    assert.equal(fs.existsSync(path.join(repo, "credentials.json")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: rename protected -> safe blocks (source in touch set)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-ren-ps-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const beforeEnv = fs.readFileSync(path.join(repo, ".env"));
  stagePatch(xdg, RUN_ID, captureRenamePatch(repo, ".env", "env-moved.txt"));
  try {
    const res = autoApply(repo, xdg);
    assertBlockedProtected(res, { expectProtectedSubstring: ".env" });
    assert.deepEqual(fs.readFileSync(path.join(repo, ".env")), beforeEnv);
    assert.equal(fs.existsSync(path.join(repo, "env-moved.txt")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: safe patch still applies (regression)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-safe-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, captureSafePatch(repo));
  try {
    const res = autoApply(repo, xdg);
    assert.equal(res.ok, true, JSON.stringify(res));
    assert.equal(res.outcome, "applied");
    assert.equal(fs.readFileSync(path.join(repo, "safe.txt"), "utf8"), "safe edited\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop auto: protected path blocks; exit 1; envelope outcome", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-peer-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePathPatch(repo, ".env", "SECRET=peer-leak\n"));
  const before = fs.readFileSync(path.join(repo, ".env"));
  const lines = [];
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], (l) =>
        lines.push(l)
      )
    );
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false);
    assert.equal(res.outcome, "blocked-protected-path");
    assert.equal(peerStopExitCode(0, res), 1);
    assert.deepEqual(fs.readFileSync(path.join(repo, ".env")), before);
    assert.ok(lines.some((l) => /protected|BLOCKED/i.test(l)), lines.join("\n"));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("peer-stop auto: safe patch still applies (regression)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-prot-peer-safe-"));
  const repo = initRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, captureSafePatch(repo));
  try {
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repo), repo, "auto", ["--target", repo], () => {})
    );
    assert.equal(res.ok, true, JSON.stringify(res));
    assert.equal(res.outcome, "applied");
    assert.equal(fs.readFileSync(path.join(repo, "safe.txt"), "utf8"), "safe edited\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
