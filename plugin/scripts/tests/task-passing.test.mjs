// plugin/scripts/tests/task-passing.test.mjs
//
// PR968 codex rescue-task-injection: proves the companion's shell-injection-safe
// task channel. Free-text task content must never sit in a shell-evaluated
// position; the safe channel is `--task-file -`, where the caller pipes the task
// on stdin (a single-quoted heredoc, passed byte-for-byte by the shell) and the
// companion stages those exact bytes into a private temp file handed to the
// wrapper as `--task-file <temp>`. These tests feed a task containing $(...) and
// backticks on stdin and assert the wrapper receives it LITERALLY -- never
// command-substituted -- and that the staged temp file is cleaned up.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { companionIsolation } from "./helpers/fake-wrapper.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(SCRIPT_DIR, "..", "grok-companion.mjs");
const ECHO_WRAPPER = path.resolve(SCRIPT_DIR, "fixtures", "echo_task_wrapper.py");

// A task laced with shell metacharacters that WOULD execute if it ever reached a
// shell: command substitution, backticks, a chained command, and a redirect.
const DANGEROUS_TASK =
  "Fix bug $(touch /tmp/grok-pwned) and `whoami`; rm -rf / > /dev/null # end\nsecond line\n";

function runCompanionWithStdin(args, stdin, extraEnv = {}) {
  const iso = companionIsolation({
    env: {
      GROK_AGENT_WRAPPER: ECHO_WRAPPER,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      GROK_PYTHON: "python3",
      ...extraEnv,
    },
  });
  try {
    const result = spawnSync(process.execPath, [COMPANION, ...args], {
      encoding: "utf8",
      input: stdin,
      cwd: iso.cwd,
      env: iso.env,
    });
    return { result, iso };
  } catch (err) {
    iso.cleanup();
    throw err;
  }
}

test("--task-file - passes stdin task to the wrapper literally, no shell evaluation", () => {
  const { result, iso } = runCompanionWithStdin(
    ["verify", "--worktree", "/x", "--task-file", "-"],
    DANGEROUS_TASK
  );
  try {
    assert.equal(result.status, 0, result.stderr);
    const envelope = JSON.parse(result.stdout.trim());
    // The wrapper received the EXACT bytes piped on stdin -- $(...) and backticks
    // intact -- proving nothing was command-substituted along the way.
    assert.equal(envelope.taskEcho, DANGEROUS_TASK);
  } finally {
    iso.cleanup();
  }
});

test("--task-file - stages a temp file the wrapper reads, then removes it", () => {
  // Snapshot ONLY the private TMPDIR we inject - never the shared os.tmpdir(),
  // which concurrent suites also use for grok-task-* staging.
  const { result, iso } = runCompanionWithStdin(
    ["verify", "--worktree", "/x", "--task-file", "-"],
    "hello task\n"
  );
  try {
    assert.equal(result.status, 0, result.stderr);
    const envelope = JSON.parse(result.stdout.trim());
    assert.equal(envelope.taskEcho, "hello task\n");
    const leftovers = fs
      .readdirSync(iso.env.TMPDIR)
      .filter((name) => name.startsWith("grok-task-"));
    assert.deepEqual(leftovers, [], `staged task dir(s) leaked: ${leftovers.join(", ")}`);
  } finally {
    iso.cleanup();
  }
});

test("a --target value with shell metacharacters reaches the wrapper as a literal argv token", () => {
  // PR968 codex argv-safe user-controlled command flags: the companion forwards
  // argv via spawn(array) with NO shell, so a hostile --target (or --base, etc.)
  // reaches the wrapper byte-for-byte and is never command-substituted at the
  // companion boundary. Locks that boundary against a future refactor to a shell
  // string. The single-quoting the command docs prescribe protects the earlier
  // model-driven shell hop; this proves the companion hop is argv-safe too.
  const iso = companionIsolation({
    env: {
      GROK_AGENT_WRAPPER: ECHO_WRAPPER,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      GROK_PYTHON: "python3",
    },
  });
  try {
    const marker = path.join(iso.env.TMPDIR, `grok-flag-pwned-${process.pid}`);
    fs.rmSync(marker, { force: true });
    const hostileTarget = `pkgs/$(touch ${marker})\`whoami\`;rm -rf x`;
    const result = spawnSync(
      process.execPath,
      [COMPANION, "code", "--target", hostileTarget, "--base", "HEAD", "--task-file", "-"],
      {
        encoding: "utf8",
        input: "implement it\n",
        cwd: iso.cwd,
        env: iso.env,
      }
    );
    assert.equal(result.status, 0, result.stderr);
    const envelope = JSON.parse(result.stdout.trim());
    assert.ok(
      Array.isArray(envelope.argv) && envelope.argv.includes(hostileTarget),
      `wrapper must receive the hostile --target literally; argv was ${JSON.stringify(envelope.argv)}`
    );
    assert.equal(
      fs.existsSync(marker),
      false,
      "the $(...) inside --target must never execute anywhere in the companion boundary"
    );
  } finally {
    iso.cleanup();
  }
});

test("without the stdin sentinel the companion forwards argv unchanged (pure passthrough)", () => {
  // A literal --task-file path (not the "-" sentinel) is forwarded as-is; the
  // wrapper reads that path directly and staging never runs.
  const iso = companionIsolation({
    env: {
      GROK_AGENT_WRAPPER: ECHO_WRAPPER,
      GROK_ALLOW_WRAPPER_OVERRIDE: "1",
      GROK_PYTHON: "python3",
    },
  });
  try {
    const taskFile = path.join(iso.cwd, "task");
    fs.writeFileSync(taskFile, "literal path task $(nope)\n", "utf8");
    const result = spawnSync(
      process.execPath,
      [COMPANION, "verify", "--worktree", "/x", "--task-file", taskFile],
      {
        encoding: "utf8",
        cwd: iso.cwd,
        env: iso.env,
      }
    );
    assert.equal(result.status, 0, result.stderr);
    const envelope = JSON.parse(result.stdout.trim());
    assert.equal(envelope.taskEcho, "literal path task $(nope)\n");
  } finally {
    iso.cleanup();
  }
});
