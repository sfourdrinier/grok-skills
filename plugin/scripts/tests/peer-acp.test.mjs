// plugin/scripts/tests/peer-acp.test.mjs
//
// Companion gate for experimental ACP peer channel.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(SCRIPT_DIR, "..", "grok-companion.mjs");

function runCompanion(args, env = {}) {
  return spawnSync(process.execPath, [COMPANION, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

test("peer modes refused without GROK_EXPERIMENTAL_ACP", () => {
  for (const mode of ["peer", "peer-start", "peer-prompt", "peer-stop"]) {
    const args =
      mode === "peer"
        ? ["peer", "start", "--target", ".", "--base", "HEAD"]
        : [mode, ...(mode === "peer-start" ? ["--target", ".", "--base", "HEAD"] : ["--run-id", "20260717T000000Z-aaaaaa"])];
    const result = runCompanion(args, { GROK_EXPERIMENTAL_ACP: "" });
    assert.notEqual(result.status, 0, mode);
    assert.match(
      result.stderr || "",
      /GROK_EXPERIMENTAL_ACP=1/,
      `stderr should mention flag for ${mode}: ${result.stderr}`
    );
    assert.match(
      result.stderr || "",
      /2026-07-17-acp-peer-channel-design/,
      `stderr should point at the spec for ${mode}`
    );
  }
});

test("peer modes refuse direct run-mode even with experimental flag", () => {
  const result = runCompanion(
    ["peer", "start", "--target", ".", "--base", "HEAD", "--run-mode", "direct"],
    { GROK_EXPERIMENTAL_ACP: "1" }
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr || "", /hardened/i);
});
