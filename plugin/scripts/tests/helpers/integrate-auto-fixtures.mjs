// plugin/scripts/tests/helpers/integrate-auto-fixtures.mjs
//
// Shared fixtures for integrate-auto and integrate-continue-run tests.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const RUN_ID = "20260717T120000Z-a1b2c3";

export function codeEnvelope(overrides = {}) {
  return JSON.stringify({
    schemaVersion: 1,
    status: "success",
    mode: "code",
    runId: RUN_ID,
    response: { text: "code-done" },
    ...overrides,
  });
}

export function handoffEnvelope(ready, overrides = {}) {
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

export function git(cwd, args) {
  const r = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(r.status, 0, `git ${args.join(" ")} failed: ${r.stderr}`);
  return r;
}

export function initTargetRepo(dir) {
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
export function capturePatchAndRestore(repo) {
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

export function stagePatch(xdgStateHome, runId, patchBody) {
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

export function companionEnv(env, root, xdg, callsPath) {
  return {
    ...env,
    XDG_STATE_HOME: xdg,
    CLAUDE_PLUGIN_DATA: path.join(root, "pdata"),
    GROK_COMPANION_EXECUTION_CONTEXT: "foreground",
    ...(callsPath ? { FAKE_WRAPPER_CALLS: callsPath } : {}),
  };
}

