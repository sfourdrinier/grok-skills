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
//
// Migrated onto helpers/fake-wrapper.mjs (echoTask responses); no bespoke fixture.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  companionIsolation,
  makeFakeWrapper,
  runCompanion,
} from "./helpers/fake-wrapper.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPANION = path.resolve(SCRIPT_DIR, "..", "grok-companion.mjs");

// A task laced with shell metacharacters that WOULD execute if it ever reached a
// shell: command substitution, backticks, a chained command, and a redirect.
const DANGEROUS_TASK =
  "Fix bug $(touch /tmp/grok-pwned) and `whoami`; rm -rf / > /dev/null # end\nsecond line\n";

/** Modes that may receive --task-file in these tests. */
function makeEchoHarness() {
  return makeFakeWrapper({
    verify: { echoTask: true },
    code: { echoTask: true },
  });
}

test("--task-file - passes stdin task to the wrapper literally, no shell evaluation", () => {
  const { env, cleanup } = makeEchoHarness();
  try {
    const res = runCompanion(
      ["verify", "--worktree", "/x", "--task-file", "-"],
      { env, stdin: DANGEROUS_TASK }
    );
    assert.equal(res.code, 0, res.stderr);
    const envelope = JSON.parse(res.stdout.trim());
    // The wrapper received the EXACT bytes piped on stdin -- $(...) and backticks
    // intact -- proving nothing was command-substituted along the way.
    assert.equal(envelope.taskEcho, DANGEROUS_TASK);
  } finally {
    cleanup();
  }
});

test("--task-file - stages a temp file the wrapper reads, then removes it", () => {
  // Snapshot ONLY the private TMPDIR we inject - never the shared os.tmpdir(),
  // which concurrent suites also use for grok-task-* staging. Keep isolation
  // open across the spawn so we can readdir TMPDIR after the run.
  const { env, cleanup } = makeEchoHarness();
  const iso = companionIsolation({ env });
  try {
    const result = spawnSync(
      process.execPath,
      [COMPANION, "verify", "--worktree", "/x", "--task-file", "-"],
      {
        encoding: "utf8",
        input: "hello task\n",
        cwd: iso.cwd,
        env: iso.env,
      }
    );
    assert.equal(result.status, 0, result.stderr);
    const envelope = JSON.parse(result.stdout.trim());
    assert.equal(envelope.taskEcho, "hello task\n");
    const leftovers = fs
      .readdirSync(iso.env.TMPDIR)
      .filter((name) => name.startsWith("grok-task-"));
    assert.deepEqual(leftovers, [], `staged task dir(s) leaked: ${leftovers.join(", ")}`);
  } finally {
    iso.cleanup();
    cleanup();
  }
});

test("a --target value with shell metacharacters reaches the wrapper as a literal argv token", () => {
  // PR968 codex argv-safe user-controlled command flags: the companion forwards
  // argv via spawn(array) with NO shell, so a hostile --target (or --base, etc.)
  // reaches the wrapper byte-for-byte and is never command-substituted at the
  // companion boundary. Locks that boundary against a future refactor to a shell
  // string. The single-quoting the command docs prescribe protects the earlier
  // model-driven shell hop; this proves the companion hop is argv-safe too.
  const { env, cleanup } = makeEchoHarness();
  const iso = companionIsolation({ env });
  try {
    const marker = path.join(iso.env.TMPDIR, `grok-flag-pwned-${process.pid}`);
    fs.rmSync(marker, { force: true });
    const hostileTarget = `pkgs/$(touch ${marker})\`whoami\`;rm -rf x`;
    const result = spawnSync(
      process.execPath,
      [
        COMPANION,
        "code",
        "--integration",
        "worktree",
        "--target",
        hostileTarget,
        "--base",
        "HEAD",
        "--task-file",
        "-",
      ],
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
    cleanup();
  }
});

test("without the stdin sentinel the companion forwards argv unchanged (pure passthrough)", () => {
  // A literal --task-file path (not the "-" sentinel) is forwarded as-is; the
  // wrapper reads that path directly and staging never runs.
  const { env, cleanup } = makeEchoHarness();
  const iso = companionIsolation({ env });
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
    cleanup();
  }
});
