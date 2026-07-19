// plugin/scripts/tests/stop-review-gate-hook.test.mjs
//
// Unit tests for the stop-gate decision parsing over canned wrapper envelopes.
// Run with: node --test plugin/scripts/tests/
//
// Kept on fixtures/fake_wrapper.py (not helpers/fake-wrapper.mjs): needs large
// stderr, PID file + SIGTERM-ignore sleep, and empty-findings success envelopes
// for the live hook spawn path (orphan / maxbuf / e2big).

import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { classifyReviewRun, decideFromEnvelope } from "../lib/gate-decision.mjs";
import { writeGateConfig } from "../lib/gate-state.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const HOOK = path.resolve(SCRIPT_DIR, "..", "stop-review-gate-hook.mjs");
// Bespoke progressive simulator - see file header for why not makeFakeWrapper.
const FAKE_WRAPPER = path.resolve(SCRIPT_DIR, "fixtures", "fake_wrapper.py");
const HOOKS_JSON = path.resolve(SCRIPT_DIR, "..", "..", "hooks", "hooks.json");

function isOrphanAlive(pid) {
  // `process.kill(pid, 0)` succeeds for a live OR a zombie (defunct) process on POSIX: a
  // SIGKILLed grandchild reparented to PID 1 lingers as a zombie until reaped, and the
  // bare signal-0 probe would count it as a FALSE live orphan and flake the orphan test.
  // Consult the scheduler state so a zombie (ps STAT beginning with "Z") -- and a
  // fully-gone process -- reads as dead, while a genuinely running (non-zombie) state
  // still reads as a live orphan.
  try {
    process.kill(pid, 0);
  } catch (err) {
    if (err && err.code === "ESRCH") return false; // no such process -> dead
    // EPERM (exists but not signalable) or any other error: fall through to the
    // state probe below, which decides definitively.
  }
  const probe = spawnSync("ps", ["-o", "stat=", "-p", String(pid)], { encoding: "utf8" });
  if (probe.status !== 0) return false; // ps found no such process -> dead
  const state = probe.stdout.trim();
  if (state === "" || state.startsWith("Z")) return false; // gone or zombie (defunct) -> dead
  return true; // a genuine, non-zombie process state -> a live orphan
}

function makeWorkspace() {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-ws-"));
  fs.mkdirSync(path.join(ws, ".git"));
  return ws;
}

// The gate now routes the review task through the companion's `--task-file -`
// stdin channel, so every runHook makes the companion stage a `grok-task-*` dir
// under os.tmpdir(). Confine that staging to a private TMPDIR so it never
// pollutes the SHARED os.tmpdir() that task-passing.test.mjs snapshots in a
// parallel test process (a cross-file isolation leak otherwise).
const STAGING_TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-staging-"));

function runHook(input, env, cwd) {
  // A large maxBuffer so the TEST can capture the hook's (forwarded) stderr; the
  // point under test is that the CHILD chain is not ENOBUFS-killed.
  return spawnSync(process.execPath, [HOOK], {
    input: JSON.stringify(input),
    encoding: "utf8",
    cwd,
    env: { ...env, TMPDIR: STAGING_TMPDIR, TMP: STAGING_TMPDIR, TEMP: STAGING_TMPDIR },
    maxBuffer: 128 * 1024 * 1024,
  });
}

test("success free-text review envelope without findings -> block", () => {
  const decision = decideFromEnvelope({ status: "success", mode: "review", runId: "r1" });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /machine-readable findings|structured/i);
});

test("success review with only low findings -> allow", () => {
  const decision = decideFromEnvelope({
    status: "success",
    mode: "review",
    response: {
      structured: {
        findings: [{ severity: "low", title: "nits" }],
        summary: "ok",
      },
    },
  });
  assert.equal(decision.ok, true);
});

test("success review with high finding -> block", () => {
  const decision = decideFromEnvelope({
    status: "success",
    mode: "review",
    response: {
      structured: {
        findings: [{ severity: "high", title: "auth bypass" }],
        summary: "bad",
      },
    },
  });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /critical\/high/i);
});

test("success verify envelope with verdict pass -> allow", () => {
  const decision = decideFromEnvelope({
    status: "success",
    mode: "verify",
    verifier: { identity: "grok-grok-4.5", verdict: "pass" }
  });
  assert.equal(decision.ok, true);
});

test("success verify envelope with verdict fail -> block", () => {
  const decision = decideFromEnvelope({
    status: "success",
    mode: "verify",
    verifier: { identity: "grok-grok-4.5", verdict: "fail" }
  });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /verify verdict is "fail"/);
});

test("success verify envelope with verdict inconclusive -> block", () => {
  const decision = decideFromEnvelope({
    status: "success",
    verifier: { identity: "grok-grok-4.5", verdict: "inconclusive" }
  });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /inconclusive/);
});

test("failure envelope -> block with error class", () => {
  const decision = decideFromEnvelope({
    status: "failure",
    error: { class: "sandbox-failure", message: "profile did not apply", detail: null }
  });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /sandbox-failure/);
});

test("auth-missing failure -> block and point at /grok:setup", () => {
  const decision = decideFromEnvelope({
    status: "failure",
    error: { class: "auth-missing", message: "no auth.json", detail: null }
  });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /\/grok:setup/);
});

test("envelope with no status -> block (malformed, actionable)", () => {
  const decision = decideFromEnvelope({ runId: "r1", mode: "review" });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /no status/);
  assert.match(decision.reason, /\/grok:setup/);
});

test("non-object envelope -> block", () => {
  assert.equal(decideFromEnvelope(null).ok, false);
  assert.equal(decideFromEnvelope("nope").ok, false);
});

test("classifyReviewRun: timeout -> block with actionable note", () => {
  const decision = classifyReviewRun({ error: { code: "ETIMEDOUT" }, status: null, stdout: "" });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /timed out/);
  assert.match(decision.reason, /--disable-review-gate/);
});

test("classifyReviewRun: empty stdout -> block, points at /grok:setup", () => {
  const decision = classifyReviewRun({ error: null, status: 3, stdout: "" });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /\/grok:setup/);
});

test("classifyReviewRun: non-ETIMEDOUT spawn error -> block with the real error, not a setup hint", () => {
  // F3 classifyReviewRun-non-ETIMEDOUT: a resource-exhaustion / spawn failure
  // must surface its real code+message, not be mis-attributed to auth/setup.
  for (const code of ["EMFILE", "ENOMEM", "EACCES", "ENOENT"]) {
    const decision = classifyReviewRun({
      error: { code, message: `spawn node ${code}` },
      status: null,
      stdout: "",
    });
    assert.equal(decision.ok, false);
    assert.match(decision.reason, new RegExp(code));
    assert.match(decision.reason, /could not spawn the review run/);
  }
});

test("classifyReviewRun: non-JSON stdout -> block malformed", () => {
  const decision = classifyReviewRun({ error: null, status: 0, stdout: "not json at all" });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /not a valid JSON envelope/);
});

test("classifyReviewRun: free-text success without findings -> block", () => {
  const decision = classifyReviewRun({
    error: null,
    status: 0,
    stdout: `${JSON.stringify({ status: "success", mode: "review", runId: "r1" })}\n`,
  });
  assert.equal(decision.ok, false);
});

test("classifyReviewRun: structured success with empty findings -> allow", () => {
  const decision = classifyReviewRun({
    error: null,
    status: 0,
    stdout: `${JSON.stringify({
      status: "success",
      mode: "review",
      runId: "r1",
      response: { structured: { findings: [], summary: "clean" } },
    })}\n`,
  });
  assert.equal(decision.ok, true);
});

test("classifyReviewRun: failure envelope on stdout (non-zero exit) -> block", () => {
  const decision = classifyReviewRun({
    error: null,
    status: 1,
    stdout: JSON.stringify({ status: "failure", error: { class: "timeout", message: "ran long", detail: null } })
  });
  assert.equal(decision.ok, false);
  assert.match(decision.reason, /timeout/);
});

function assertAllowStdout(stdout, message) {
  // Allow is either empty (legacy Claude Code) or explicit {"continue":true}
  // for Codex Stop (which requires JSON on exit 0). Never a block decision.
  const trimmed = String(stdout ?? "").trim();
  if (trimmed === "") return;
  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    assert.fail(`${message}: allow stdout must be empty or JSON, got ${trimmed}`);
  }
  assert.notEqual(parsed.decision, "block", message);
  assert.equal(parsed.continue, true, message);
}

test("F-GATE-RECURSE: stop_hook_active short-circuits (allow) without running the review", () => {
  const ws = makeWorkspace();
  const pluginData = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-data-"));
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-xdg-"));
  const env = {
    ...process.env,
    CLAUDE_PLUGIN_DATA: pluginData,
    GROK_AGENT_WRAPPER: FAKE_WRAPPER,
    GROK_ALLOW_WRAPPER_OVERRIDE: "1",
    GROK_PYTHON: "python3",
    XDG_STATE_HOME: xdg,
    // If the recursion guard were broken and the review DID run, this fake would
    // exit non-zero and the gate would emit a block. The guard must prevent the run.
    GROK_FAKE_EXIT: "1",
  };
  writeGateConfig(ws, true, env); // gate ENABLED

  const result = runHook({ stop_hook_active: true, cwd: ws }, env, ws);
  assert.equal(result.status, 0);
  assertAllowStdout(result.stdout, "no review runs and no block decision is emitted");
  assert.match(result.stderr, /stop_hook_active/);
});

test("F-GATE-MAXBUF: a large wrapper stderr does not trip a spurious block", () => {
  const ws = makeWorkspace();
  const pluginData = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-data-"));
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-xdg-"));
  const env = {
    ...process.env,
    CLAUDE_PLUGIN_DATA: pluginData,
    GROK_AGENT_WRAPPER: FAKE_WRAPPER,
    GROK_ALLOW_WRAPPER_OVERRIDE: "1",
    GROK_PYTHON: "python3",
    XDG_STATE_HOME: xdg,
    // ~3MB of stderr: well over Node's ~1MB default spawn buffer. The gate must
    // inherit (not capture) the child stderr so this cannot ENOBUFS-kill the run.
    GROK_FAKE_STDERR_BYTES: String(3 * 1024 * 1024),
  };
  writeGateConfig(ws, true, env); // gate ENABLED

  const result = runHook({ cwd: ws }, env, ws);
  assert.equal(result.status, 0);
  // The wrapper's success envelope is honored (allow): no block decision despite
  // the large stderr; a spurious ENOBUFS block would write decision:block.
  assertAllowStdout(result.stdout, "a valid success envelope must not be blocked by large stderr");
});

test("F-GATE-FAILCLOSED: a review run that dies with empty stdout blocks (never implicit allow)", () => {
  // The task is now delivered on the child's stdin (F-GATE-E2BIG), so a NUL byte
  // in the task no longer sits in argv and no longer forces a synchronous spawn
  // throw. The invariant under test is unchanged: a failed review that produces
  // NO envelope on stdout must fail closed with a block, never an empty-stdout
  // implicit ALLOW. We simulate that with a wrapper that exits non-zero and
  // prints nothing, exercising the end-to-end empty-stdout fail-closed path.
  const ws = makeWorkspace();
  const pluginData = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-data-"));
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-xdg-"));
  const emptyWrapperDir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-emptywrap-"));
  const emptyWrapper = path.join(emptyWrapperDir, "empty_wrapper.py");
  fs.writeFileSync(emptyWrapper, "import sys\nsys.exit(1)\n", "utf8");
  const env = {
    ...process.env,
    CLAUDE_PLUGIN_DATA: pluginData,
    GROK_AGENT_WRAPPER: emptyWrapper,
    GROK_ALLOW_WRAPPER_OVERRIDE: "1",
    GROK_PYTHON: "python3",
    XDG_STATE_HOME: xdg,
  };
  writeGateConfig(ws, true, env); // gate ENABLED

  const result = runHook({ cwd: ws, last_assistant_message: "anything" }, env, ws);
  assert.notEqual(result.stdout.trim(), "", "must never exit with empty stdout (implicit allow) on a failed run");
  const decision = JSON.parse(result.stdout);
  assert.equal(decision.decision, "block", "a failed run with no envelope fails closed with a block");
  assert.match(decision.reason, /\/grok:setup/);
});

test("F-GATE-E2BIG: a very large stop-gate task is passed via stdin and classifies without E2BIG", () => {
  // Regression guard: a large previous-turn response makes the review task exceed
  // the OS argument-list limit. If the task were a `--task <text>` argv element,
  // spawnSync would fail with E2BIG (argument-list-too-long) and the gate would
  // spuriously block. Routing the task through the companion's `--task-file -`
  // stdin channel removes the ceiling: the gate still runs and classifies the
  // wrapper's success envelope as an allow. 4 MiB is well over ARG_MAX and the
  // Linux per-argument MAX_ARG_STRLEN, so the old argv path would have thrown.
  const ws = makeWorkspace();
  const pluginData = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-data-"));
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-xdg-"));
  const env = {
    ...process.env,
    CLAUDE_PLUGIN_DATA: pluginData,
    GROK_AGENT_WRAPPER: FAKE_WRAPPER,
    GROK_ALLOW_WRAPPER_OVERRIDE: "1",
    GROK_PYTHON: "python3",
    XDG_STATE_HOME: xdg,
  };
  writeGateConfig(ws, true, env); // gate ENABLED

  const hugeTask = "x".repeat(4 * 1024 * 1024);
  const result = runHook({ cwd: ws, last_assistant_message: hugeTask }, env, ws);
  assert.equal(result.error, undefined, "the gate must not fail to spawn (no E2BIG) on a huge task");
  assert.equal(result.status, 0, result.stderr);
  assertAllowStdout(
    result.stdout,
    "the huge task reaches the wrapper via stdin and its success envelope is allowed (no spurious block)"
  );
});

test("F-GATE-ORPHAN: a gate timeout group-kills the wrapper grandchild (no orphan)", { skip: process.platform === "win32" }, async () => {
  const ws = makeWorkspace();
  const pluginData = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-data-"));
  const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-xdg-"));
  const pidFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "grok-gate-pid-")), "wrapper.pid");
  const env = {
    ...process.env,
    CLAUDE_PLUGIN_DATA: pluginData,
    GROK_AGENT_WRAPPER: FAKE_WRAPPER,
    GROK_ALLOW_WRAPPER_OVERRIDE: "1",
    GROK_PYTHON: "python3",
    XDG_STATE_HOME: xdg,
    // The wrapper records its PID then ignores SIGTERM and sleeps well past the
    // gate timeout, so ONLY the gate's process-GROUP SIGKILL can end it.
    GROK_FAKE_PID_FILE: pidFile,
    GROK_FAKE_IGNORE_SIGTERM: "1",
    GROK_FAKE_SLEEP: "60",
    // Shorten the gate timeout (clamped to the default) so the timeout path fires
    // deterministically in the test.
    GROK_STOP_REVIEW_TIMEOUT_MS: "2000",
  };
  writeGateConfig(ws, true, env); // gate ENABLED

  const result = runHook({ cwd: ws }, env, ws);
  // The timeout is classified as a block (fail closed), not an allow.
  assert.notEqual(result.stdout.trim(), "");
  assert.equal(JSON.parse(result.stdout).decision, "block");

  // The wrapper grandchild recorded its PID before sleeping; after the group-kill it must
  // be dead. A SIGKILLed grandchild can linger as a zombie reparented to PID 1 -- and
  // process.kill(pid,0) SUCCEEDS for a zombie -- so isOrphanAlive consults the process
  // state and treats a defunct (Z) process as dead. Poll briefly for the async reap.
  assert.ok(fs.existsSync(pidFile), "the wrapper grandchild must have recorded its PID");
  const wrapperPid = Number(fs.readFileSync(pidFile, "utf8").trim());
  assert.ok(Number.isInteger(wrapperPid) && wrapperPid > 0, "a valid wrapper PID was recorded");

  let alive = true;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (!isOrphanAlive(wrapperPid)) {
      alive = false;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.equal(alive, false, "the wrapper grandchild must be group-killed, not left as an orphan");
});

test("F-GATE-ORPHAN-ZOMBIE: a defunct (zombie) child is treated as dead by the liveness probe", { skip: process.platform === "win32" }, async () => {
  // Regression guard for isOrphanAlive: create a REAL zombie -- a python parent forks a
  // child that exits immediately while the parent stays alive WITHOUT reaping it, so the
  // child is defunct (ps STAT "Z"). process.kill(pid,0) alone would wrongly report it as
  // a live orphan; the state-aware probe must classify it as dead. Keeps the orphan test
  // meaningful: a genuinely running (non-zombie) process still reads as alive.
  const py = process.env.GROK_PYTHON || "python3";
  const forkSource = [
    "import os, sys, time",
    "pid = os.fork()",
    "if pid == 0:",
    "    os._exit(0)",  // child exits -> becomes a zombie (parent never waits)
    "sys.stdout.write(str(pid) + '\\n')",
    "sys.stdout.flush()",
    "time.sleep(30)",
  ].join("\n");
  const parent = spawn(py, ["-c", forkSource], { stdio: ["ignore", "pipe", "ignore"] });
  try {
    const zombiePid = await new Promise((resolve, reject) => {
      let buf = "";
      parent.stdout.on("data", (chunk) => {
        buf += chunk.toString("utf8");
        const nl = buf.indexOf("\n");
        if (nl >= 0) resolve(Number(buf.slice(0, nl).trim()));
      });
      parent.on("error", reject);
      parent.on("exit", () => reject(new Error("python parent exited before printing the child pid")));
    });
    assert.ok(Number.isInteger(zombiePid) && zombiePid > 0, "a valid zombie pid was recorded");

    // Poll briefly for the child to transition to defunct, then require the probe to
    // report it dead despite process.kill(pid,0) still succeeding for a zombie.
    let deadByProbe = false;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      if (!isOrphanAlive(zombiePid)) {
        deadByProbe = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(deadByProbe, true, "a zombie/defunct child must be treated as dead, not a live orphan");
  } finally {
    parent.kill("SIGKILL");
  }
});

test("F-GATE-TIMEOUT: the gate's internal timeout is strictly less than the harness timeout", () => {
  const hookSrc = fs.readFileSync(HOOK, "utf8");
  const internalMatch = /STOP_REVIEW_TIMEOUT_MS\s*=\s*([0-9]+)\s*\*\s*1000/.exec(hookSrc);
  assert.ok(internalMatch, "could not find STOP_REVIEW_TIMEOUT_MS in the hook source");
  const internalSeconds = Number(internalMatch[1]);

  const hooksJson = JSON.parse(fs.readFileSync(HOOKS_JSON, "utf8"));
  const harnessSeconds = hooksJson.hooks.Stop[0].hooks[0].timeout;
  assert.ok(Number.isFinite(harnessSeconds), "hooks.json Stop timeout must be a number");

  assert.ok(
    internalSeconds < harnessSeconds,
    `internal gate timeout ${internalSeconds}s must be strictly less than the harness ${harnessSeconds}s`
  );
});
