// plugin/scripts/tests/task-file.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  extractTask,
  stageTaskFile,
  injectTaskFile,
  stageStdinTaskFile,
} from "../lib/task-file.mjs";
import { runCompanion } from "./helpers/fake-wrapper.mjs";

test("extractTask reads the equals-form --task=... and --task-file=...", () => {
  // The hardened wrapper's argparse accepts both forms; the direct path must too.
  assert.equal(extractTask(["code", "--task=fix the bug", "--target", "."]), "fix the bug");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-task-eq-"));
  try {
    const f = path.join(tmp, "task.md");
    fs.writeFileSync(f, "task from file\n");
    assert.equal(extractTask(["code", `--task-file=${f}`, "--target", "."]), "task from file\n");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("extractTask reads --task inline text", () => {
  assert.equal(extractTask(["code", "--task", "do the thing", "--target", "."]), "do the thing");
});

test("extractTask prefers --task-file path content over --task", () => {
  const { taskPath, cleanup } = stageTaskFile("from-file");
  try {
    assert.equal(
      extractTask(["code", "--task", "inline", "--task-file", taskPath]),
      "from-file"
    );
  } finally {
    cleanup();
  }
});

test("extractTask last-wins for duplicate --task (split and equals)", () => {
  // Wrapper argparse last-wins; first-wins would silently run the wrong task.
  assert.equal(
    extractTask(["code", "--task", "first", "--task", "second", "--target", "."]),
    "second"
  );
  assert.equal(
    extractTask(["code", "--task=first", "--task=second", "--target", "."]),
    "second"
  );
  assert.equal(
    extractTask(["code", "--task", "first", "--task=second", "--target", "."]),
    "second"
  );
});

test("extractTask last-wins for duplicate --task-file and still outranks --task", () => {
  const a = stageTaskFile("file-a");
  const b = stageTaskFile("file-b");
  try {
    assert.equal(
      extractTask(["code", "--task-file", a.taskPath, "--task-file", b.taskPath]),
      "file-b",
      "last --task-file must win"
    );
    assert.equal(
      extractTask([
        "code",
        `--task-file=${a.taskPath}`,
        "--task",
        "inline-should-lose",
        `--task-file=${b.taskPath}`,
      ]),
      "file-b",
      "last task-file still outranks any --task (cross-flag policy)"
    );
    // Cross-flag policy preserved: even a later --task loses to any real task-file.
    assert.equal(
      extractTask(["code", "--task-file", a.taskPath, "--task", "later-inline"]),
      "file-a"
    );
  } finally {
    a.cleanup();
    b.cleanup();
  }
});

test("extractTask returns empty for missing file or stdin sentinel", () => {
  assert.equal(extractTask(["code", "--task-file", "-"]), "");
  assert.equal(extractTask(["code", "--task-file", "/no/such/path-xyz"]), "");
  assert.equal(extractTask(["code", "--target", "."]), "");
});

test("stageTaskFile writes 0600 file and cleanup removes it", () => {
  const { taskPath, cleanup } = stageTaskFile("hello task");
  assert.equal(fs.readFileSync(taskPath, "utf8"), "hello task");
  assert.equal(fs.statSync(taskPath).mode & 0o777, 0o600);
  cleanup();
  assert.equal(fs.existsSync(taskPath), false);
});

test("injectTaskFile strips old task flags and appends staged file", () => {
  const { args, cleanup } = injectTaskFile(["code", "--task", "old", "--target", "."], "new text");
  assert.equal(args[0], "code");
  assert.ok(!args.includes("old"));
  const tf = args.indexOf("--task-file");
  assert.ok(tf > 0);
  assert.equal(fs.readFileSync(args[tf + 1], "utf8"), "new text");
  cleanup();
});

test("injectTaskFile strips an existing --task-file path pair", () => {
  const prior = stageTaskFile("prior payload");
  try {
    const { args, cleanup } = injectTaskFile(
      ["reason", "--task-file", prior.taskPath, "--target", "."],
      "replacement text"
    );
    assert.equal(args[0], "reason");
    assert.ok(!args.includes(prior.taskPath));
    const tf = args.indexOf("--task-file");
    assert.ok(tf > 0);
    assert.equal(fs.readFileSync(args[tf + 1], "utf8"), "replacement text");
    assert.notEqual(args[tf + 1], prior.taskPath);
    cleanup();
  } finally {
    prior.cleanup();
  }
});

test("injectTaskFile strips equals-form --task=/--task-file= tokens", () => {
  const { args, cleanup } = injectTaskFile(
    ["code", "--task=old inline", "--target", "."],
    "replacement text"
  );
  assert.equal(args[0], "code");
  assert.ok(!args.some((a) => a.startsWith("--task=")));
  const tfEq = args.filter((a) => a.startsWith("--task-file=")).length;
  assert.equal(tfEq, 0);
  const tf = args.indexOf("--task-file");
  assert.ok(tf > 0);
  assert.equal(fs.readFileSync(args[tf + 1], "utf8"), "replacement text");
  cleanup();
});

test("injectTaskFile does not consume a following flag as --task/--task-file value", () => {
  // Regression: bare `--task` before `--target` used to swallow `--target`.
  const { args, cleanup } = injectTaskFile(
    ["code", "--task", "--target", ".", "--web"],
    "new text"
  );
  assert.ok(args.includes("--target"), args.join(" "));
  assert.ok(args.includes("."), args.join(" "));
  assert.ok(args.includes("--web"), args.join(" "));
  assert.ok(!args.includes("old"));
  const tf = args.indexOf("--task-file");
  assert.ok(tf > 0);
  assert.equal(fs.readFileSync(args[tf + 1], "utf8"), "new text");
  cleanup();
});

test("stageStdinTaskFile returns null when argv has no --task-file - sentinel", () => {
  assert.equal(stageStdinTaskFile(["code", "--target", "."]), null);
  assert.equal(stageStdinTaskFile(["code", "--task-file", "/tmp/x"]), null);
  assert.equal(stageStdinTaskFile(["code", "--task-file=/tmp/x"]), null);
  assert.equal(stageStdinTaskFile(["code", "--task", "inline"]), null);
});

test("stageStdinTaskFile last-wins: later real path clears earlier stdin sentinel", () => {
  // When the LAST --task-file is a real path, an earlier stdin sentinel must not
  // trigger staging (returns null; no process.stdin read).
  assert.equal(
    stageStdinTaskFile(["code", "--task-file", "-", "--task-file", "/tmp/real-task.md"]),
    null
  );
  assert.equal(
    stageStdinTaskFile(["code", "--task-file=-", "--task-file=/tmp/real-task.md"]),
    null
  );
  assert.equal(
    stageStdinTaskFile(["code", "--task-file", "-", "--task-file=/tmp/real-task.md"]),
    null
  );
});

test("stageStdinTaskFile last-wins: later stdin sentinel stages over earlier path", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stdin-last-"));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stdin-lastcwd-"));
  const prior = path.join(tmp, "prior.md");
  fs.writeFileSync(prior, "PRIOR_SHOULD_NOT_WIN\n");
  const payload = "last-wins stdin sentinel body";
  const echoBody = `import sys
args = sys.argv[1:]
path = None
for i, a in enumerate(args):
    if a == "--task-file" and i + 1 < len(args):
        path = args[i + 1]
    if a.startswith("--task-file="):
        path = a.split("=", 1)[1]
if not path:
    sys.stderr.write("missing --task-file\\n")
    sys.exit(2)
with open(path, "r", encoding="utf-8") as f:
    sys.stdout.write(f.read())
sys.exit(0)
`;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-fake-echo-last-"));
  const wrapperPath = path.join(dir, "grok_agent.py");
  fs.writeFileSync(wrapperPath, echoBody, { mode: 0o600 });
  try {
    const res = runCompanion(["reason", "--task-file", prior, "--task-file", "-"], {
      env: {
        GROK_AGENT_WRAPPER: wrapperPath,
        GROK_ALLOW_WRAPPER_OVERRIDE: "1",
        TMPDIR: tmp,
      },
      cwd,
      stdin: payload,
    });
    assert.equal(res.code, 0, res.stderr);
    assert.equal(res.stdout.trim(), payload);
    assert.doesNotMatch(res.stdout, /PRIOR_SHOULD_NOT_WIN/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("stageStdinTaskFile happy path: stdin bytes staged and content matches end to end", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stdin-stage-"));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stdin-cwd-"));
  const payload = "sensitive staged stdin task body";
  // Fake wrapper echoes the --task-file path content so we can assert staging
  // correctness without calling stageStdinTaskFile directly (stdin is process-bound).
  const echoBody = `import sys
args = sys.argv[1:]
path = None
for i, a in enumerate(args):
    if a == "--task-file" and i + 1 < len(args):
        path = args[i + 1]
        break
if not path:
    sys.stderr.write("missing --task-file\\n")
    sys.exit(2)
with open(path, "r", encoding="utf-8") as f:
    sys.stdout.write(f.read())
sys.exit(0)
`;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-fake-echo-"));
  const wrapperPath = path.join(dir, "grok_agent.py");
  fs.writeFileSync(wrapperPath, echoBody, { mode: 0o600 });
  try {
    const res = runCompanion(["reason", "--task-file", "-"], {
      env: {
        GROK_AGENT_WRAPPER: wrapperPath,
        GROK_ALLOW_WRAPPER_OVERRIDE: "1",
        TMPDIR: tmp,
      },
      cwd,
      stdin: payload,
    });
    assert.equal(res.code, 0);
    assert.equal(res.stdout.trim(), payload);
    // Staging dir under TMPDIR must be gone after companion exit.
    const leftovers = fs.readdirSync(tmp).filter((d) => d.startsWith("grok-task-"));
    assert.deepEqual(leftovers, []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("stageStdinTaskFile equals form: --task-file=- also stages stdin end to end", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stdin-eq-"));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-stdin-eqcwd-"));
  const payload = "equals-form staged stdin body";
  // Echo wrapper handles BOTH --task-file <path> and --task-file=<path>.
  const echoBody = `import sys
args = sys.argv[1:]
path = None
for i, a in enumerate(args):
    if a == "--task-file" and i + 1 < len(args):
        path = args[i + 1]
        break
    if a.startswith("--task-file="):
        path = a.split("=", 1)[1]
        break
if not path:
    sys.stderr.write("missing --task-file\\n")
    sys.exit(2)
with open(path, "r", encoding="utf-8") as f:
    sys.stdout.write(f.read())
sys.exit(0)
`;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-fake-echo-eq-"));
  const wrapperPath = path.join(dir, "grok_agent.py");
  fs.writeFileSync(wrapperPath, echoBody, { mode: 0o600 });
  try {
    const res = runCompanion(["reason", "--task-file=-"], {
      env: {
        GROK_AGENT_WRAPPER: wrapperPath,
        GROK_ALLOW_WRAPPER_OVERRIDE: "1",
        TMPDIR: tmp,
      },
      cwd,
      stdin: payload,
    });
    assert.equal(res.code, 0, res.stderr);
    assert.equal(res.stdout.trim(), payload);
    const leftovers = fs.readdirSync(tmp).filter((d) => d.startsWith("grok-task-"));
    assert.deepEqual(leftovers, []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test("reason --input= equals form injects hermetic --no-web", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "grok-reason-input-eq-"));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "grok-reason-input-eqcwd-"));
  // Fake wrapper dumps argv so we can assert companion appended --no-web.
  const echoBody = `import sys
print(" ".join(sys.argv[1:]))
sys.exit(0)
`;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "grok-fake-echo-input-"));
  const wrapperPath = path.join(dir, "grok_agent.py");
  fs.writeFileSync(wrapperPath, echoBody, { mode: 0o600 });
  try {
    const res = runCompanion(["reason", "--input=art.md", "--task", "summarize"], {
      env: {
        GROK_AGENT_WRAPPER: wrapperPath,
        GROK_ALLOW_WRAPPER_OVERRIDE: "1",
        TMPDIR: tmp,
      },
      cwd,
    });
    assert.equal(res.code, 0, res.stderr);
    assert.match(res.stdout, /--no-web/, `expected hermetic --no-web for --input=; got: ${res.stdout}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});
