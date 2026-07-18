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
  peerStopExitCode,
} from "../lib/integrate.mjs";

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

test("peer-stop auto: consent gate keys on the peer's repository, not cwd", () => {
  // Started for repoB; stopped from repoA (cwd) with no --target. Direct mode
  // must read repoB's consent, not repoA's (which we grant to prove it's ignored).
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-peer-int-consent-"));
  const repoA = initRepo(path.join(root, "repoA"));
  const repoB = initRepo(path.join(root, "repoB"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatch(repoB));
  const lines = [];
  try {
    // No consent recorded for repoB -> direct apply must be refused (not applied).
    const res = withXdg(xdg, () =>
      maybeIntegratePeerStop(peerStopEnvelope(repoB), repoA, "direct", [], (l) => lines.push(l))
    );
    assert.equal(res.outcome, "consent-required");
    // Fail closed: a requested-but-blocked direct apply is attempted+not-ok, so
    // peerStopExitCode surfaces a nonzero exit rather than the wrapper's 0.
    assert.equal(res.attempted, true);
    assert.equal(res.ok, false);
    assert.equal(peerStopExitCode(0, res), 1);
    assert.equal(
      fs.readFileSync(path.join(repoB, "foo.txt"), "utf8"),
      "hello\n",
      "repoB must be untouched without repoB consent"
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
