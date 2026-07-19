// plugin/scripts/tests/subagent-stop-hook.test.mjs
//
// SubagentStop handoff-nudge hook: matching grok-engineer-coder + unconsumed
// code runId emits additionalContext; garbage / non-match is silent exit 0.
// Run with: node --test (cwd plugin/scripts)

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { createJob, updateJob } from "../lib/jobs.mjs";
import { runsDirFor } from "../progress-relay.mjs";
import {
  isPeerRunId,
  messageLooksLikePeerRun,
  reminderContextForAgent,
} from "../subagent-stop-hook.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const HOOK = path.resolve(SCRIPT_DIR, "..", "subagent-stop-hook.mjs");
const HOOKS_JSON = path.resolve(SCRIPT_DIR, "..", "..", "hooks", "hooks.json");

const VALID_RUN_ID = "20260717T120000Z-abcdef";
const VALID_RUN_ID_OLDER = "20260717T110000Z-fedcba";

function makeWorkspace() {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-subagent-stop-"));
  fs.mkdirSync(path.join(cwd, ".git"));
  return cwd;
}

function seedEnv(cwd) {
  const stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "grok-subagent-xdg-"));
  const pluginData = path.join(cwd, "pdata");
  return {
    ...process.env,
    CLAUDE_PLUGIN_DATA: pluginData,
    XDG_STATE_HOME: stateHome,
  };
}

function seedCodeJob(cwd, env, runId, { kind = "code", sleepMs = 0 } = {}) {
  if (sleepMs > 0) {
    // Ensure updatedAt ordering is distinct across rapid creates.
    const end = Date.now() + sleepMs;
    while (Date.now() < end) {
      /* busy wait for ms-level timestamp separation */
    }
  }
  const job = createJob(cwd, { kind, mode: kind, runMode: "hardened" }, env);
  updateJob(cwd, job.id, { runId, status: "success", summary: "ok" }, env);
  return job;
}

function seedRunDir(env, runId, { consumed = false } = {}) {
  const runDir = path.join(runsDirFor(env), runId);
  fs.mkdirSync(runDir, { recursive: true });
  if (consumed) {
    fs.writeFileSync(path.join(runDir, "handoff-consumed.json"), "{}\n", "utf8");
  }
  return runDir;
}

function runHook(input, env, { rawStdin } = {}) {
  return spawnSync(process.execPath, [HOOK], {
    input: rawStdin !== undefined ? rawStdin : JSON.stringify(input),
    encoding: "utf8",
    env,
    cwd: typeof input === "object" && input && input.cwd ? input.cwd : process.cwd(),
  });
}

function subagentStopPayload(cwd, agentType = "grok:grok-engineer-coder") {
  return {
    last_assistant_message: "code run finished",
    agent_id: "agent-abc",
    agent_type: agentType,
    session_id: "session-1",
    transcript_path: path.join(cwd, "transcript.jsonl"),
    cwd,
    hook_event_name: "SubagentStop",
  };
}

test("matching agent_type + unconsumed code runId emits handoff additionalContext", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  seedRunDir(env, VALID_RUN_ID);

  const res = runHook(subagentStopPayload(cwd), env);
  assert.equal(res.status, 0, `expected exit 0, got ${res.status}; stderr=${res.stderr}`);
  const out = JSON.parse(res.stdout.trim());
  assert.equal(typeof out.hookSpecificOutput?.additionalContext, "string");
  // No runId in last_assistant_message -> fallback wording (newest unconsumed).
  assert.match(
    out.hookSpecificOutput.additionalContext,
    new RegExp(`most recent code run in this workspace: ${VALID_RUN_ID}`)
  );
  assert.match(out.hookSpecificOutput.additionalContext, /handoff --run-id /);
  assert.match(out.hookSpecificOutput.additionalContext, new RegExp(VALID_RUN_ID));
  assert.match(out.hookSpecificOutput.additionalContext, /dual-condition ready/);
  assert.match(out.hookSpecificOutput.additionalContext, /never auto-apply/);
});
test("plugin-scoped agent_type suffix :grok-engineer-coder also matches", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  seedRunDir(env, VALID_RUN_ID);

  const res = runHook(subagentStopPayload(cwd, "my-org:grok-engineer-coder"), env);
  assert.equal(res.status, 0);
  assert.match(res.stdout, new RegExp(VALID_RUN_ID));
  assert.match(res.stdout, /hookSpecificOutput/);
});

test("bare agent_type grok-engineer-coder also matches", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  seedRunDir(env, VALID_RUN_ID);

  const res = runHook(subagentStopPayload(cwd, "grok-engineer-coder"), env);
  assert.equal(res.status, 0);
  assert.match(res.stdout, new RegExp(VALID_RUN_ID));
  assert.match(res.stdout, /hookSpecificOutput/);
});

test("message-embedded runId wins over a newer unrelated code job", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  // Older job whose runId is cited in last_assistant_message.
  seedCodeJob(cwd, env, VALID_RUN_ID_OLDER);
  seedRunDir(env, VALID_RUN_ID_OLDER);
  // Newer unrelated code job (would win under newest-unconsumed fallback alone).
  seedCodeJob(cwd, env, VALID_RUN_ID, { sleepMs: 5 });
  seedRunDir(env, VALID_RUN_ID);

  const payload = subagentStopPayload(cwd);
  payload.last_assistant_message =
    `Implementation done. Envelope runId ${VALID_RUN_ID_OLDER} ready for handoff.`;
  const res = runHook(payload, env);
  assert.equal(res.status, 0, `stderr=${res.stderr}`);
  const out = JSON.parse(res.stdout.trim());
  assert.match(out.hookSpecificOutput.additionalContext, new RegExp(VALID_RUN_ID_OLDER));
  assert.doesNotMatch(
    out.hookSpecificOutput.additionalContext,
    new RegExp(VALID_RUN_ID)
  );
  // Direct association keeps the specific-run wording (not the softened fallback).
  assert.match(
    out.hookSpecificOutput.additionalContext,
    new RegExp(`Grok code run ${VALID_RUN_ID_OLDER} finished`)
  );
  assert.doesNotMatch(
    out.hookSpecificOutput.additionalContext,
    /most recent code run in this workspace/
  );
});

test("fallback wording softens when no message runId matches", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  seedRunDir(env, VALID_RUN_ID);

  const payload = subagentStopPayload(cwd);
  payload.last_assistant_message = "done with the work, no run id here";
  const res = runHook(payload, env);
  assert.equal(res.status, 0);
  const out = JSON.parse(res.stdout.trim());
  assert.match(
    out.hookSpecificOutput.additionalContext,
    new RegExp(`most recent code run in this workspace: ${VALID_RUN_ID}`)
  );
  assert.match(out.hookSpecificOutput.additionalContext, /handoff --run-id /);
  assert.match(out.hookSpecificOutput.additionalContext, new RegExp(VALID_RUN_ID));
});
test("newest unconsumed code runId wins when multiple exist", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID_OLDER);
  seedRunDir(env, VALID_RUN_ID_OLDER);
  seedCodeJob(cwd, env, VALID_RUN_ID, { sleepMs: 5 });
  seedRunDir(env, VALID_RUN_ID);

  const res = runHook(subagentStopPayload(cwd), env);
  assert.equal(res.status, 0);
  const out = JSON.parse(res.stdout.trim());
  assert.match(out.hookSpecificOutput.additionalContext, new RegExp(VALID_RUN_ID));
  assert.doesNotMatch(
    out.hookSpecificOutput.additionalContext,
    new RegExp(VALID_RUN_ID_OLDER)
  );
});

test("consumed handoff marker skips that run; silent if none left", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  seedRunDir(env, VALID_RUN_ID, { consumed: true });

  const res = runHook(subagentStopPayload(cwd), env);
  assert.equal(res.status, 0);
  assert.equal(res.stdout.trim(), "");
});

test("non-grok agent_type is silent exit 0", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  seedRunDir(env, VALID_RUN_ID);

  const res = runHook(subagentStopPayload(cwd, "Explore"), env);
  assert.equal(res.status, 0);
  assert.equal(res.stdout.trim(), "");
});

test("garbage stdin is silent exit 0", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  const res = runHook(null, env, { rawStdin: "not-json{{{" });
  assert.equal(res.status, 0);
  assert.equal(res.stdout.trim(), "");
});

test("missing run dir is silent exit 0", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  // intentionally no seedRunDir

  const res = runHook(subagentStopPayload(cwd), env);
  assert.equal(res.status, 0);
  assert.equal(res.stdout.trim(), "");
});

test("hooks.json registers SubagentStop with timeout 5 and handoff statusMessage", () => {
  const hooksJson = JSON.parse(fs.readFileSync(HOOKS_JSON, "utf8"));
  const group = hooksJson.hooks?.SubagentStop;
  assert.ok(Array.isArray(group) && group.length > 0, "SubagentStop must be registered");
  const cmd = group[0].hooks[0];
  assert.equal(cmd.type, "command");
  assert.match(cmd.command, /subagent-stop-hook\.mjs/);
  assert.equal(cmd.timeout, 5);
  assert.equal(cmd.statusMessage, "Grok handoff reminder");
});


test("messageLooksLikePeerRun detects peer channel wording", () => {
  assert.equal(messageLooksLikePeerRun("peer stop --run-id x finished"), true);
  assert.equal(messageLooksLikePeerRun("peer-stop ready"), true);
  assert.equal(messageLooksLikePeerRun("peer start launched"), true);
  assert.equal(messageLooksLikePeerRun("code run finished handoff next"), false);
});

test("reminderContextForAgent peer mode never tells handoff/apply", () => {
  const peer = reminderContextForAgent(VALID_RUN_ID, { peer: true });
  assert.match(peer, /peer-stop/);
  assert.doesNotMatch(peer, /handoff --run-id/);
  assert.match(peer, /Never auto-apply|never auto-apply/i);
  const code = reminderContextForAgent(VALID_RUN_ID, { fromMessage: true });
  assert.match(code, /handoff --run-id/);
});

test("peer runId (peer.json) nudges peer-stop outcome, not handoff", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID);
  const runDir = seedRunDir(env, VALID_RUN_ID);
  fs.writeFileSync(path.join(runDir, "peer.json"), "{}\n");

  const payload = subagentStopPayload(cwd);
  payload.last_assistant_message = `peer stop complete for ${VALID_RUN_ID}`;
  const res = runHook(payload, env);
  assert.equal(res.status, 0, res.stderr);
  const out = JSON.parse(res.stdout.trim());
  assert.match(out.hookSpecificOutput.additionalContext, /peer-stop/);
  assert.doesNotMatch(out.hookSpecificOutput.additionalContext, /handoff --run-id/);
  assert.equal(isPeerRunId(VALID_RUN_ID, cwd, env), true);
});

// RED / reviewer blocker: durable code job + run dir must still emit the code
// handoff nudge even when the assistant message mentions peer-stop. Peer
// classification requires durable peer.json or peer job kind; wording alone
// cannot override durable code evidence.
test("durable code job/run + peer-stop wording still emits code handoff nudge", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID, { kind: "code" });
  seedRunDir(env, VALID_RUN_ID); // no peer.json

  const payload = subagentStopPayload(cwd);
  payload.last_assistant_message =
    `code finished for ${VALID_RUN_ID}; note: peer-stop is for peer channel only`;
  const res = runHook(payload, env);
  assert.equal(res.status, 0, res.stderr);
  assert.ok(res.stdout.trim(), "expected additionalContext for durable code run");
  const out = JSON.parse(res.stdout.trim());
  assert.match(
    out.hookSpecificOutput.additionalContext,
    /handoff --run-id/,
    "durable code evidence must keep handoff nudge"
  );
  assert.match(out.hookSpecificOutput.additionalContext, new RegExp(VALID_RUN_ID));
  assert.doesNotMatch(
    out.hookSpecificOutput.additionalContext,
    /Use the peer-stop outcome/,
    "peer-stop wording alone must not flip durable code to peer"
  );
  assert.equal(isPeerRunId(VALID_RUN_ID, cwd, env), false);
});

// Peer job kind (no peer.json) is durable peer evidence.
test("durable peer job kind (no peer.json) nudges peer-stop, not handoff", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  seedCodeJob(cwd, env, VALID_RUN_ID, { kind: "peer" });
  seedRunDir(env, VALID_RUN_ID); // no peer.json

  const payload = subagentStopPayload(cwd);
  payload.last_assistant_message = `peer stop complete for ${VALID_RUN_ID}`;
  const res = runHook(payload, env);
  assert.equal(res.status, 0, res.stderr);
  const out = JSON.parse(res.stdout.trim());
  assert.match(out.hookSpecificOutput.additionalContext, /peer-stop/);
  assert.doesNotMatch(out.hookSpecificOutput.additionalContext, /handoff --run-id/);
  assert.equal(isPeerRunId(VALID_RUN_ID, cwd, env), true);
});

// Wording may classify as peer only when there is no durable code job evidence
// (run dir exists with a validated runId, but jobs index has no kind=code row).
test("peer-stop wording classifies peer only without durable code job evidence", () => {
  const cwd = makeWorkspace();
  const env = seedEnv(cwd);
  // Run dir only - no jobs row of kind code (and no peer.json).
  seedRunDir(env, VALID_RUN_ID);

  const payload = subagentStopPayload(cwd);
  payload.last_assistant_message = `peer-stop ready for ${VALID_RUN_ID}`;
  const res = runHook(payload, env);
  assert.equal(res.status, 0, res.stderr);
  const out = JSON.parse(res.stdout.trim());
  assert.match(out.hookSpecificOutput.additionalContext, /peer-stop/);
  assert.doesNotMatch(out.hookSpecificOutput.additionalContext, /handoff --run-id/);
});
