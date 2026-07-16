// plugin/scripts/tests/grok-companion-relay.test.mjs
//
// Integration tests for the T2-2 relay wiring in grok-companion.mjs, driven by
// the fake wrapper fixture (no real Grok). They prove the degrade-to-Tier-1
// state machine's three timings each deliver the wrapper's envelope EXACTLY ONCE
// and unchanged on stdout, that the relay never writes stdout, and that live
// progress surfaces on stderr. Run with: node --test plugin/scripts/tests/

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { parseRunIdMarker } from "../progress-relay.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(SCRIPT_DIR, "..", "grok-companion.mjs");
const FAKE_WRAPPER = path.resolve(SCRIPT_DIR, "fixtures", "fake_wrapper.py");

function runCompanion(args, extraEnv) {
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-relay-xdg-"));
  const result = spawnSync(process.execPath, [COMPANION, ...args], {
    encoding: "utf8",
    env: {
      ...process.env,
      GROK_AGENT_WRAPPER: FAKE_WRAPPER,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      GROK_PYTHON: "python3",
      XDG_STATE_HOME: xdg,
      ...extraEnv,
    },
  });
  return { result, xdg };
}

function stdoutJsonLines(stdout) {
  return stdout
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

test("streaming success: envelope is the sole stdout line, exit 0, progress on stderr", () => {
  // GROK_FAKE_SLEEP gives the live poll a chance; the final drain covers the rest.
  const { result } = runCompanion(["reason", "--task", "ping"], { GROK_FAKE_SLEEP: "0.3" });
  assert.equal(result.status, 0);

  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1, "stdout must carry exactly one envelope line (relay never writes stdout)");
  const envelope = JSON.parse(lines[0]);
  assert.equal(envelope.status, "success");
  assert.equal(envelope.mode, "reason");

  // The streamed thought token surfaced as human-readable progress on stderr.
  assert.match(result.stderr, /\[grok\] grok: grok streamed thought tokens/);
  assert.match(result.stderr, /thinking about PONG/);
});

test("run-id marker is forwarded to stderr and the relay follows the announced run past a decoy", () => {
  // F-RELAY-RUNID: the wrapper announces its run id on stderr; the companion
  // forwards that line verbatim AND uses it to bind the relay to the exact run,
  // even when a lexically-newer decoy run dir also appears.
  const { result } = runCompanion(["reason", "--task", "ping"], {
    GROK_FAKE_SLEEP: "0.3",
    GROK_FAKE_DECOY_RUN_ID: "20990101T000000Z-ffffff",
  });
  assert.equal(result.status, 0);

  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1, "stdout stays exactly one envelope line");
  const envelope = JSON.parse(lines[0]);
  assert.equal(envelope.status, "success");

  // The marker line was forwarded verbatim, and it named the wrapper's real run.
  assert.match(result.stderr, /\[grok-run-id\] \d{8}T\d{6}Z-[0-9a-f]{6}/);
  assert.equal(parseRunIdMarker(`[grok-run-id] ${envelope.runId}`), envelope.runId);
  // The real run's live progress surfaced (the relay bound to the announced run).
  assert.match(result.stderr, /thinking about PONG/);
});

test("degrade (i) no stream at start: envelope still delivered exactly once, exit 0", () => {
  const { result } = runCompanion(["reason", "--task", "ping"], { GROK_FAKE_BEHAVIOR: "norun" });
  assert.equal(result.status, 0);

  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).status, "success");
});

test("degrade (ii) unreadable progress mid-run: envelope still delivered exactly once", () => {
  const { result } = runCompanion(["reason", "--task", "ping"], {
    GROK_FAKE_BEHAVIOR: "brokenprogress",
    GROK_FAKE_SLEEP: "0.2",
  });
  assert.equal(result.status, 0);

  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1, "a failing relay must not add or corrupt stdout");
  assert.equal(JSON.parse(lines[0]).status, "success");
});

test("degrade (iii) run fails: failure envelope delivered verbatim, exit 1", () => {
  const { result } = runCompanion(["code", "--target", ".", "--base", "HEAD", "--task", "x"], {
    GROK_FAKE_EXIT: "1",
  });
  assert.equal(result.status, 1);

  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1);
  const envelope = JSON.parse(lines[0]);
  assert.equal(envelope.status, "failure");
  assert.equal(envelope.error.class, "cli-failure");
});

test("status renders a prior run's progress on stderr with a verbatim envelope on stdout", () => {
  // Pre-create the run the status read-back will inspect.
  const runId = "20260715T050000Z-abcdef";
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-relay-status-"));
  const runDir = path.join(xdg, "grok-skills", "runs", runId);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "progress.jsonl"),
    `${JSON.stringify({ seq: 1, phase: "start", level: "info", message: "reason run created" })}\n` +
      `${JSON.stringify({ seq: 2, phase: "grok", level: "info", message: "grok streamed thought tokens", data: { text: "recalling the answer" } })}\n`
  );

  const result = spawnSync(process.execPath, [COMPANION, "status", "--run-id", runId], {
    encoding: "utf8",
    env: {
      ...process.env,
      GROK_AGENT_WRAPPER: FAKE_WRAPPER,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      GROK_PYTHON: "python3",
      XDG_STATE_HOME: xdg,
      GROK_FAKE_BEHAVIOR: "norun",
    },
  });

  assert.equal(result.status, 0);
  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1, "status stdout stays the verbatim envelope");
  assert.equal(JSON.parse(lines[0]).mode, "status");

  assert.match(result.stderr, /reason run created/);
  assert.match(result.stderr, /recalling the answer/);
});

test("status: an invalid/partial run renders NO progress, only the wrapper's failure envelope", () => {
  // PR968 codex status-render-order: a strict-shaped run dir holding ONLY a
  // progress file (no valid/owned run.json) is one the wrapper rejects as
  // invalid-target (exit != 0). The relay must NOT surface its progress content
  // -- the render is gated on the wrapper's SUCCESS envelope, so validation is
  // never bypassed.
  const runId = "20260715T060000Z-abcdef";
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-relay-status-bad-"));
  const runDir = path.join(xdg, "grok-skills", "runs", runId);
  fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(
    path.join(runDir, "progress.jsonl"),
    `${JSON.stringify({ seq: 1, phase: "grok", level: "info", message: "leaked partial run content" })}\n`
  );

  // norun: the fake wrapper leaves the pre-planted dir untouched. EXIT 1: it
  // reports a failure envelope, standing in for the real wrapper's invalid-target.
  const result = spawnSync(process.execPath, [COMPANION, "status", "--run-id", runId], {
    encoding: "utf8",
    env: {
      ...process.env,
      GROK_AGENT_WRAPPER: FAKE_WRAPPER,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      GROK_PYTHON: "python3",
      XDG_STATE_HOME: xdg,
      GROK_FAKE_BEHAVIOR: "norun",
      GROK_FAKE_EXIT: "1",
    },
  });

  assert.equal(result.status, 1);
  const lines = stdoutJsonLines(result.stdout);
  assert.equal(lines.length, 1, "status stdout stays the verbatim failure envelope");
  assert.equal(JSON.parse(lines[0]).status, "failure");
  // The pre-planted progress content must never reach the terminal.
  assert.doesNotMatch(result.stderr, /leaked partial run content/);
});
