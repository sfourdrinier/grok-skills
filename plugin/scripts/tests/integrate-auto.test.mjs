// plugin/scripts/tests/integrate-auto.test.mjs
//
// Task 7.3: integration=auto - apply-on-verified-ready with apply-time revalidation.
// Real temp git target so apply is exercised on disk (fake-wrapper for code/handoff).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  attachIntegrationFinalOutcome,
  buildAutoFinalEnvelope,
  buildPeerStopFinalEnvelope,
  companionIntegrationToWrapper,
  restForWrapperIntegration,
} from "../lib/implement.mjs";
import { applyVerifiedPatch, locateImplementationPatch } from "../lib/integrate.mjs";
import { makeFakeWrapper, readCalls, runCompanion } from "./helpers/fake-wrapper.mjs";

const RUN_ID = "20260717T120000Z-a1b2c3";

function codeEnvelope(overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: "success",
    mode: "code",
    runId: RUN_ID,
    response: { text: "code-done" },
    ...overrides,
  });
}

function handoffEnvelope(ready, overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: ready ? "success" : "failure",
    mode: "handoff",
    runId: RUN_ID,
    response: {
      integration: {
        ready,
        blockers: ready ? [] : [{ kind: "handoff-unavailable" }],
      },
    },
    ...overrides,
  });
}

function git(cwd, args) {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(r.status, 0, `git ${args.join(" ")} failed: ${r.stderr}`);
  return r;
}

function initTargetRepo(dir) {
  fs.mkdirSync(dir, { recursive: true });
  git(dir, ["init"]);
  git(dir, ["config", "user.email", "test@example.com"]);
  git(dir, ["config", "user.name", "Test"]);
  fs.writeFileSync(path.join(dir, "foo.txt"), "hello\n");
  git(dir, ["add", "foo.txt"]);
  git(dir, ["commit", "-m", "init"]);
  return dir;
}

/** Modify foo.txt, capture binary diff vs HEAD, restore original so apply can land. */
function capturePatchAndRestore(repo) {
  const file = path.join(repo, "foo.txt");
  const original = fs.readFileSync(file, "utf8");
  fs.writeFileSync(file, "hello world\n");
  const diff = spawnSync("git", ["diff", "--binary", "HEAD"], {
    cwd: repo,
    encoding: "utf8",
  });
  assert.equal(diff.status, 0, diff.stderr);
  assert.ok(diff.stdout.includes("foo.txt"), "patch must mention foo.txt");
  fs.writeFileSync(file, original);
  return diff.stdout;
}

function stagePatch(xdgStateHome, runId, patchBody) {
  const runDir = path.join(xdgStateHome, "grok-skills", "runs", runId);
  const art = path.join(runDir, "artifacts");
  fs.mkdirSync(art, { recursive: true });
  const patchPath = path.join(art, "implementation.patch");
  const buf = Buffer.from(patchBody);
  fs.writeFileSync(patchPath, buf);
  // Matching validation manifest: apply re-verifies patch bytes/sha256 against
  // this after apply-time handoff ready (same SSOT peer uses).
  const sha = createHash("sha256").update(buf).digest("hex");
  fs.writeFileSync(
    path.join(runDir, "implementation-handoff.json"),
    JSON.stringify({
      patch: {
        sha256: sha,
        bytes: buf.length,
        relativePath: "artifacts/implementation.patch",
      },
    })
  );
  return patchPath;
}

function companionEnv(env, root, xdg, callsPath) {
  return {
    ...env,
    XDG_STATE_HOME: xdg,
    CLAUDE_PLUGIN_DATA: path.join(root, "pdata"),
    GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
    ...(callsPath ? { FAKE_WRAPPER_CALLS: callsPath } : {}),
  };
}

test("applyVerifiedPatch: an unreadable patch is a BLOCKED outcome, not a throw", () => {
  // The patch can vanish/become unreadable between locateImplementationPatch's
  // stat and the sha hash. That race must return a blocked outcome (so auto still
  // builds its final envelope + finalizes the job), never an uncaught exception.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-unread-"));
  const xdg = path.join(root, "xdg");
  const patchPath = stagePatch(xdg, RUN_ID, "diff --git a/x b/x\n+z\n");
  fs.chmodSync(patchPath, 0o000);
  let readable = true;
  try {
    fs.readFileSync(patchPath);
  } catch {
    readable = false;
  }
  try {
    if (readable) return; // running as root (perm bypass) -> cannot exercise EACCES
    const res = applyVerifiedPatch({
      wrapper: "unused",
      runId: RUN_ID,
      targetRepo: root,
      env: { XDG_STATE_HOME: xdg },
      // handoff revalidation reports ready so we reach the patch-hash step.
      runHandoff: () => ({ code: 0, envelope: { response: { integration: { ready: true } } } }),
      stderrLine: () => {},
    });
    assert.equal(res.ok, false);
    assert.equal(res.outcome, "blocked-patch-unreadable");
  } finally {
    try {
      fs.chmodSync(patchPath, 0o644);
    } catch {
      /* ignore */
    }
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("unit: buildAutoFinalEnvelope carries handoff fields + apply outcome", () => {
  const result = {
    ready: true,
    handoffEnvelope: {
      schemaVersion: 1,
      mode: "handoff",
      runId: "r",
      status: "success",
      response: { integration: { ready: true, blockers: [] } },
    },
  };
  const env = buildAutoFinalEnvelope(result, 0, { ok: true, outcome: "applied" });
  assert.equal(env.status, "success");
  // The terminal envelope for a `code --integration auto` COMMAND keys as "code",
  // not the handoff envelope it was built from (callers dispatch on envelope.mode).
  assert.equal(env.mode, "code");
  assert.equal(env.runId, "r");
  assert.equal(env.response.integration.ready, true);
  assert.equal(env.response.integration.applied, true);
  assert.equal(env.response.integration.outcome, "applied");
  // failed apply -> status failure, applied false, the real apply outcome.
  const env2 = buildAutoFinalEnvelope(result, 1, { ok: false, outcome: "blocked-dirty-overlap" });
  assert.equal(env2.status, "failure");
  assert.equal(env2.response.integration.applied, false);
  assert.equal(env2.response.integration.outcome, "blocked-dirty-overlap");
  // no apply (not-ready) -> applied false, outcome not-ready.
  const env3 = buildAutoFinalEnvelope({ ready: false, handoffEnvelope: result.handoffEnvelope }, 1, null);
  assert.equal(env3.response.integration.applied, false);
  assert.equal(env3.response.integration.outcome, "not-ready");
  // no handoff envelope (no runId) -> null so the caller falls back to code stdout.
  assert.equal(buildAutoFinalEnvelope({ handoffEnvelope: null }, 1, null), null);
});

test("unit: attachIntegrationFinalOutcome + buildPeerStopFinalEnvelope share the SSOT", () => {
  const base = {
    schemaVersion: 1,
    mode: "peer-stop",
    status: "success",
    runId: "peer-r",
    response: {
      peer: { integrationReady: true },
      integration: { ready: true, blockers: [] },
    },
  };
  const attached = attachIntegrationFinalOutcome(
    base,
    1,
    { ok: false, outcome: "consent-required" },
    { readyFallback: true }
  );
  assert.equal(attached.status, "failure");
  assert.equal(attached.mode, "peer-stop", "peer mode is preserved unless overridden");
  assert.equal(attached.response.integration.ready, true);
  assert.equal(attached.response.integration.applied, false);
  assert.equal(attached.response.integration.outcome, "consent-required");

  const blocked = buildPeerStopFinalEnvelope(base, 1, {
    attempted: true,
    ok: false,
    outcome: "blocked-dirty-overlap",
  });
  assert.equal(blocked.status, "failure");
  assert.equal(blocked.response.integration.applied, false);
  assert.equal(blocked.response.integration.outcome, "blocked-dirty-overlap");

  const applied = buildPeerStopFinalEnvelope(base, 0, {
    attempted: true,
    ok: true,
    outcome: "applied",
  });
  assert.equal(applied.status, "success");
  assert.equal(applied.response.integration.applied, true);
  assert.equal(applied.response.integration.outcome, "applied");

  // Retained (review) is not an apply success: applied stays false.
  const retained = buildPeerStopFinalEnvelope(base, 0, {
    attempted: false,
    ok: true,
    outcome: "retained",
  });
  assert.equal(retained.status, "success");
  assert.equal(retained.response.integration.applied, false);
  assert.equal(retained.response.integration.outcome, "retained");

  assert.equal(buildPeerStopFinalEnvelope(null, 1, null), null);
});

test("unit: companionIntegrationToWrapper maps auto/review to worktree", () => {
  assert.equal(companionIntegrationToWrapper("direct"), "direct");
  assert.equal(companionIntegrationToWrapper("worktree"), "worktree");
  assert.equal(companionIntegrationToWrapper("auto"), "worktree");
  assert.equal(companionIntegrationToWrapper("review"), "worktree");
});

test("unit: restForWrapperIntegration rewrites auto to worktree", () => {
  assert.deepEqual(
    restForWrapperIntegration(["--integration", "auto", "--target", ".", "--task", "x"]),
    ["--target", ".", "--task", "x", "--integration", "worktree"]
  );
  assert.deepEqual(
    restForWrapperIntegration(["--integration", "review", "--target", "r"]),
    ["--target", "r", "--integration", "worktree"]
  );
});

test("unit: locateImplementationPatch resolves under XDG state root", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-locate-patch-"));
  const xdg = path.join(root, "xdg");
  try {
    const p = stagePatch(xdg, RUN_ID, "diff --git a/x b/x\n");
    const found = locateImplementationPatch(RUN_ID, { XDG_STATE_HOME: xdg });
    assert.equal(found, p);
    assert.equal(
      locateImplementationPatch("20990101T000000Z-dead00", { XDG_STATE_HOME: xdg }),
      null
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto happy path: applies ready patch to real temp target; exit 0", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-happy-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);
  const before = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
  assert.equal(before, "hello\n");

  const callsPath = path.join(root, "calls.log");
  const ready = handoffEnvelope(true);
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    // First handoff (post-code) + second (apply-time revalidation)
    handoff: { stdout: `${ready}\n`, exitCode: 0 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "apply me",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 0, `stderr: ${res.stderr}\nstdout: ${res.stdout}`);
    const after = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
    assert.equal(after, "hello world\n", "target file must change on disk");
    assert.match(res.stderr, /APPLIED|applied/i);
    const calls = readCalls(callsPath);
    assert.deepEqual(calls, ["code", "handoff", "handoff"]);
    // auto is a `code` run -> EXACTLY ONE stdout envelope (not code + handoff).
    const envLines = res.stdout.split("\n").filter((l) => l.trim().startsWith("{"));
    assert.equal(envLines.length, 1, `auto must emit one envelope; got: ${res.stdout}`);
    const finalEnv = JSON.parse(envLines[0]);
    assert.equal(finalEnv.status, "success");
    assert.equal(finalEnv.mode, "code", "auto command envelope must key as mode code");
    assert.equal(finalEnv.response?.integration?.applied, true);
    assert.equal(finalEnv.response?.integration?.outcome, "applied");
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: a malformed (non-JSON) handoff envelope yields a FAILURE stdout envelope, not code-success", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-badhandoff-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatchAndRestore(repo));
  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: { stdout: "not json at all\n", exitCode: 0 }, // unparseable
  });
  try {
    const res = runCompanion(
      ["code", "--integration", "auto", "--target", ".", "--base", "HEAD", "--task", "x"],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `stderr: ${res.stderr}`);
    const envLines = res.stdout.split("\n").filter((l) => l.trim().startsWith("{"));
    assert.equal(envLines.length, 1, `one envelope; got: ${res.stdout}`);
    const finalEnv = JSON.parse(envLines[0]);
    // Must NOT report the code leg's success; must be an honest failure.
    assert.equal(finalEnv.status, "failure", res.stdout);
    assert.equal(finalEnv.mode, "code");
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto not-ready: no apply; target unchanged; exit 1", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-notready-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);

  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: { stdout: `${handoffEnvelope(false)}\n`, exitCode: 1 },
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "nope",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "target must be unchanged when not ready"
    );
    assert.deepEqual(readCalls(callsPath), ["code", "handoff"]);
    assert.ok(
      !/APPLIED runId=/i.test(res.stderr),
      "must not claim applied when not ready"
    );
    // ONE stdout envelope, and it reflects the NOT-READY outcome (not a stale
    // SUCCESS code envelope).
    const envLines = res.stdout.split("\n").filter((l) => l.trim().startsWith("{"));
    assert.equal(envLines.length, 1, `auto must emit one envelope; got: ${res.stdout}`);
    const finalEnv = JSON.parse(envLines[0]);
    assert.equal(finalEnv.status, "failure");
    assert.equal(finalEnv.response?.integration?.ready, false);
    // /grok:result must show that same not-ready envelope + exit 1 (not the code
    // leg's SUCCESS envelope).
    const resultRes = runCompanion(["result"], {
      cwd: repo,
      env: companionEnv(env, root, xdg, callsPath),
    });
    assert.equal(resultRes.code, 1, `result exit; stderr: ${resultRes.stderr}`);
    const shown = JSON.parse(resultRes.stdout.trim());
    assert.equal(shown.response?.integration?.ready, false, resultRes.stdout);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto apply-time revalidation: tree mutation blocks half-apply; exit 1", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-reval-mut-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);
  const foo = path.join(repo, "foo.txt");

  const callsPath = path.join(root, "calls.log");
  const ready = `${handoffEnvelope(true)}\n`;
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    // 2nd handoff mutates target so git apply --check fails
    handoff: [
      { stdout: ready, exitCode: 0 },
      {
        stdout: ready,
        exitCode: 0,
        mutate: { path: foo, content: "diverged base content\n" },
      },
    ],
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "mutate race",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected blocked exit 1; stderr: ${res.stderr}`);
    // Mutate ran; apply must NOT have partially applied "hello world"
    const content = fs.readFileSync(foo, "utf8");
    assert.notEqual(content, "hello world\n", "must not half-apply the patch");
    assert.equal(
      content,
      "diverged base content\n",
      "target stays at the mutated pre-apply content (no apply)"
    );
    assert.match(res.stderr, /BLOCKED|apply --check|PARTIAL|blocked/i);
    assert.ok(!/APPLIED runId=/i.test(res.stderr), "must not claim ready-applied");
    assert.deepEqual(readCalls(callsPath), ["code", "handoff", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto dirty-overlap: blocks apply when a patch path is already dirty", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-dirty-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatchAndRestore(repo));
  // Operator is actively editing the same file the patch touches.
  fs.appendFileSync(path.join(repo, "foo.txt"), "operator work in progress\n");
  const before = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
  try {
    const res = applyVerifiedPatch({
      wrapper: "unused",
      runId: RUN_ID,
      targetRepo: repo,
      runHandoff: () => ({ code: 0, envelope: { response: { integration: { ready: true } } } }),
      stderrLine: () => {},
      env: { XDG_STATE_HOME: xdg },
    });
    assert.equal(res.ok, false);
    assert.equal(res.outcome, "blocked-dirty-overlap");
    assert.deepEqual(res.overlap, ["foo.txt"]);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      before,
      "target must be untouched when dirty-overlap blocks"
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: blocks (fail-closed) when the pre-apply dirty status cannot be read", () => {
  // A failed/killed `git status` (e.g. maxBuffer overflow in a very dirty tree)
  // yields an incomplete dirty set, and `git apply --check` alone can pass on a
  // non-conflicting hunk in a dirty file. Simulate the unreadable status with a
  // non-git targetRepo (git status exits 128) and assert we block, never apply.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-nostatus-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  stagePatch(xdg, RUN_ID, capturePatchAndRestore(repo));
  const notARepo = path.join(root, "not-a-repo");
  fs.mkdirSync(notARepo);
  try {
    const res = applyVerifiedPatch({
      wrapper: "unused",
      runId: RUN_ID,
      targetRepo: notARepo,
      runHandoff: () => ({ code: 0, envelope: { response: { integration: { ready: true } } } }),
      stderrLine: () => {},
      env: { XDG_STATE_HOME: xdg },
    });
    assert.equal(res.ok, false);
    assert.equal(res.outcome, "blocked-dirty-status");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto re-check flips to not-ready at apply time: no apply; exit 1", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-reval-flip-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchBody = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, patchBody);

  const callsPath = path.join(root, "calls.log");
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: [
      { stdout: `${handoffEnvelope(true)}\n`, exitCode: 0 },
      { stdout: `${handoffEnvelope(false)}\n`, exitCode: 1 },
    ],
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "flip",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "target unchanged when revalidation flips not-ready"
    );
    assert.match(res.stderr, /revalidation|not ready|BLOCKED/i);
    assert.ok(!/APPLIED runId=/i.test(res.stderr));
    assert.deepEqual(readCalls(callsPath), ["code", "handoff", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auto: patch tampered after apply-time ready is refused (integrity check)", () => {
  // TOCTOU: apply-time handoff revalidation returns ready against the staged
  // patch+manifest, then the on-disk implementation.patch is substituted before
  // git apply. Auto must compare current bytes/size/hash against the revalidated
  // manifest (verifyPatchAgainstManifest) and fail closed - never apply.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "grok-auto-integ-"));
  const repo = initTargetRepo(path.join(root, "repo"));
  const xdg = path.join(root, "xdg");
  const patchPath = stagePatch(xdg, RUN_ID, capturePatchAndRestore(repo));
  const before = fs.readFileSync(path.join(repo, "foo.txt"), "utf8");
  assert.equal(before, "hello\n");

  // Unit path: handoff callback reports ready, then tampers the patch so the
  // later pre-apply integrity check sees a mismatch against the manifest.
  const unit = applyVerifiedPatch({
    wrapper: "unused",
    runId: RUN_ID,
    targetRepo: repo,
    env: { XDG_STATE_HOME: xdg },
    runHandoff: () => {
      fs.writeFileSync(patchPath, `${fs.readFileSync(patchPath, "utf8")}\n# injected\n`);
      return { code: 0, envelope: { response: { integration: { ready: true } } } };
    },
    stderrLine: () => {},
  });
  assert.equal(unit.ok, false);
  assert.equal(unit.outcome, "patch-integrity-failure");
  assert.equal(
    fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
    before,
    "unit path: tampered patch must not be applied"
  );

  // Companion path: second (apply-time) handoff mutates the patch after ready.
  // Restore a matching patch first so the first handoff + stage stay honest.
  const cleanPatch = capturePatchAndRestore(repo);
  stagePatch(xdg, RUN_ID, cleanPatch);
  const patchFile = path.join(
    xdg,
    "grok-skills",
    "runs",
    RUN_ID,
    "artifacts",
    "implementation.patch"
  );
  const callsPath = path.join(root, "calls.log");
  const ready = `${handoffEnvelope(true)}\n`;
  const { env, cleanup } = makeFakeWrapper({
    code: { stdout: `${codeEnvelope()}\n`, exitCode: 0 },
    handoff: [
      { stdout: ready, exitCode: 0 },
      {
        stdout: ready,
        exitCode: 0,
        mutate: {
          path: patchFile,
          content: `${cleanPatch}\n# injected after ready\n`,
        },
      },
    ],
  });
  try {
    const res = runCompanion(
      [
        "code",
        "--integration",
        "auto",
        "--target",
        ".",
        "--base",
        "HEAD",
        "--task",
        "tamper after ready",
      ],
      { cwd: repo, env: companionEnv(env, root, xdg, callsPath) }
    );
    assert.equal(res.code, 1, `expected exit 1; stderr: ${res.stderr}`);
    assert.equal(
      fs.readFileSync(path.join(repo, "foo.txt"), "utf8"),
      "hello\n",
      "companion path: tampered patch must not be applied"
    );
    assert.ok(!/APPLIED runId=/i.test(res.stderr), "must not claim applied");
    assert.match(res.stderr, /integrity|BLOCKED/i);
    const envLines = res.stdout.split("\n").filter((l) => l.trim().startsWith("{"));
    assert.equal(envLines.length, 1, `one envelope; got: ${res.stdout}`);
    const finalEnv = JSON.parse(envLines[0]);
    assert.equal(finalEnv.status, "failure");
    assert.equal(finalEnv.mode, "code");
    assert.equal(finalEnv.response?.integration?.applied, false);
    assert.equal(finalEnv.response?.integration?.outcome, "patch-integrity-failure");
    assert.deepEqual(readCalls(callsPath), ["code", "handoff", "handoff"]);
  } finally {
    cleanup();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
