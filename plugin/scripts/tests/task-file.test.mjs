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

test("stageStdinTaskFile returns null when argv has no --task-file - sentinel", () => {
  assert.equal(stageStdinTaskFile(["code", "--target", "."]), null);
  assert.equal(stageStdinTaskFile(["code", "--task-file", "/tmp/x"]), null);
  assert.equal(stageStdinTaskFile(["code", "--task", "inline"]), null);
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
